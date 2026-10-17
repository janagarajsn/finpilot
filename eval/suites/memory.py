"""Memory suite (BUILD_PROMPT §Evaluation 8), plus chat answer quality in full mode.

Long-term memory (both modes; offline = lexical retrieval, full = Mesh embeddings):
- retrieval hit@1 / hit@5 on 20 synthetic questions over 16 stored facts (eval/datasets/memory_facts.yml),
  phrased like a user would ask (often without the fact's words)
- contradiction supersede: a later fact on the same topic replaces the earlier one in recall
- deletion removes recall; blocked content (account/card numbers, credentials, OTPs) is never stored;
  another user recalls nothing (isolation)

Full mode only:
- memory writer (fast LLM) on 4 synthetic chats: recall of the expected memories, precision
  (stored memories that match an expectation), no figures FinPilot quoted and no account numbers stored
- chat agent on the product's sample data (10 questions): share of answers whose numbers all
  passed the grounding guard (no fallback), tool use, routes, latency
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from sqlalchemy import text

from eval import loader
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import generate
from eval.synth.persona_sample import load_sample
from eval.synth.personas import get_persona
from finpilot.db.session import get_engine, user_session
from finpilot.fx.service import FXService
from finpilot.memory import writer
from finpilot.memory.long_term import MemoryIn, PgMemory
from finpilot.memory.retrieval import recall

log = logging.getLogger(__name__)
SUITE = "memory"
DATA = Path(__file__).resolve().parents[1] / "datasets" / "memory_facts.yml"
K = 5


SCOPE_INTENTS = ("off_topic", "smalltalk")


def _user(ctx: SuiteContext, label: str, sample: bool = False) -> uuid.UUID:
    ds = generate(dataclasses.replace(get_persona("inr_salaried_emi_sip"), name=f"mem_{label}"))
    ld = loader.load(ds, ctx.run_tag, txns=ds.txns[:1])
    if sample:
        with user_session(ld.user_id) as s:
            s.execute(
                text("UPDATE users SET timezone = 'Asia/Kolkata' WHERE id = :u"), {"u": ld.user_id}
            )
            load_sample(s, ld.user_id, None)
    return ld.user_id


def _store(uid: uuid.UUID, llm: Any, facts: list[dict[str, Any]]) -> dict[str, uuid.UUID]:
    ids = {}
    with user_session(uid) as s:
        mem = PgMemory(llm)
        for f in facts:
            out = mem.write(
                s,
                uid,
                MemoryIn(
                    kind=f["kind"],
                    topic=f["topic"],
                    text=f["text"],
                    importance=float(f.get("importance", 0.5)),
                    source="eval",
                ),
            )
            assert out.id is not None
            ids[f["topic"]] = out.id
    return ids


def _recall(uid: uuid.UUID, q: str, llm: Any) -> list[dict[str, Any]]:
    with user_session(uid) as s:
        return recall(s, uid, q, llm, k=K)


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    data = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    llm = ctx.llm if ctx.mode == "full" else None
    via = "vector" if llm is not None else "lexical"
    text_of = {f["topic"]: f["text"] for f in data["facts"]}
    details: dict[str, Any] = {"retrieval": via}
    metrics: dict[str, float] = {}
    try:
        # ------------------------------------------------------------ retrieval
        a = _user(ctx, "memory_a")
        ids = _store(a, llm, data["facts"])
        h1 = h5 = 0
        for q in data["questions"]:
            got = _recall(a, q["q"], llm)
            texts = [g["text"] for g in got]
            want = text_of[q["expected"]]
            rank = texts.index(want) + 1 if want in texts else None
            h1 += rank == 1
            h5 += rank is not None
            res.cases.append(
                Case(
                    f"recall:{q['q'][:60]}",
                    {"question": q["q"]},
                    {"memory": want},
                    {"recalled": texts, "rank": rank},
                    rank is not None,
                    score=1.0 / rank if rank else 0.0,
                    error_type=None if rank else "not_recalled",
                )
            )
        n = len(data["questions"])
        metrics["hit_at_1"], metrics[f"hit_at_{K}"] = h1 / n, h5 / n
        # ------------------------------------------------------------ supersede
        ok = 0
        for c in data["contradictions"]:
            with user_session(a) as s:
                out = PgMemory(llm).write(
                    s, a, MemoryIn(kind=c["kind"], topic=c["topic"], text=c["new"], source="eval")
                )
            texts = [g["text"] for g in _recall(a, c["q"], llm)]
            good = out.action == "superseded" and c["new"] in texts and c["old"] not in texts
            ok += good
            res.cases.append(
                Case(
                    f"supersede:{c['topic']}",
                    {"old": c["old"], "new": c["new"]},
                    {"recalled": c["new"], "not": c["old"]},
                    {"action": out.action, "recalled": texts},
                    good,
                    error_type=None if good else "supersede",
                )
            )
        metrics["supersede_correct"] = ok / len(data["contradictions"])
        # ------------------------------------------------------------ deletion
        gone = 0
        first: dict[str, dict[str, Any]] = {}  # one probe per topic (the first question about it)
        for q in data["questions"]:
            first.setdefault(q["expected"], q)
        probes = [first[t] for t in ("netflix", "gym", "wedding", "medical", "kids_school")]
        for q in probes:
            with user_session(a) as s:
                mid = next(
                    m["id"] for m in PgMemory().list_all(s, a) if m["topic"] == q["expected"]
                )
                PgMemory().delete(s, a, mid)
            texts = [g["text"] for g in _recall(a, q["q"], llm)]
            good = text_of[q["expected"]] not in texts
            gone += good
            res.cases.append(
                Case(
                    f"delete:{q['expected']}",
                    {"question": q["q"]},
                    {"absent": text_of[q["expected"]]},
                    {"recalled": texts},
                    good,
                    error_type=None if good else "deleted_still_recalled",
                )
            )
        metrics["deletion_removes_recall"] = gone / len(probes)
        # ------------------------------------------------------------ blocked + isolation
        with user_session(a) as s:
            acts = [
                PgMemory(llm)
                .write(s, a, MemoryIn(kind="fact", topic="x", text=t, source="eval"))
                .action
                for t in data["blocked"]
            ]
        metrics["blocked_rejected"] = sum(x == "blocked" for x in acts) / len(acts)
        b = _user(ctx, "memory_b")
        leaked = sum(bool(_recall(b, q["q"], llm)) for q in data["questions"][:8])
        metrics["cross_user_isolation"] = 1.0 - leaked / 8
        del ids
        # ------------------------------------------------------------ scope rules (both modes)
        from finpilot.agents import intent_router as router

        in_scope = [*data["chat_questions"], *data["scope"]["in_scope"]]
        refused = [q for q in in_scope if router.by_rules(router.normalize(q)) in SCOPE_INTENTS]
        metrics["scope_rules_false_refusal"] = len(refused) / len(in_scope)
        caught = [q for q in data["scope"]["off_topic"] if router.by_rules(router.normalize(q))]
        details["scope_rules"] = {"refused_in_scope": refused, "off_topic_caught": len(caught)}
        # ------------------------------------------------------------ full: writer + chat
        if llm is not None:
            metrics |= _writer(ctx, data, llm, res, details)
            metrics |= _chat(ctx, data, llm, res, details)
    finally:
        loader.cleanup(ctx.run_tag)

    for k, v in metrics.items():
        res.metrics.append(
            Metric(
                k,
                float(v),
                "ratio" if not k.endswith("_ms") else "ms",
                {"scope": "overall", "primary": True, "retrieval": via},
            )
        )
    res.details = details | {
        "questions": len(data["questions"]),
        "facts": len(data["facts"]),
        "k": K,
    }
    return res


def _writer(
    ctx: SuiteContext, data: dict[str, Any], llm: Any, res: SuiteResult, details: dict[str, Any]
) -> dict[str, float]:
    c = _user(ctx, "memory_writer")
    expected = matched_expect = stored = matched_stored = clean = 0
    for i, chat in enumerate(data["chats"]):
        with user_session(c) as s:
            before = {m["id"] for m in PgMemory().list_all(s, c)}
            sid = s.execute(
                text("INSERT INTO chat_sessions (user_id, title) VALUES (:u, :t) RETURNING id"),
                {"u": c, "t": f"eval chat {i}"},
            ).scalar_one()
            for line in chat["transcript"].strip().splitlines():
                role, _, content = line.partition(": ")
                s.execute(
                    text(
                        "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (:u, :s, :r, :c)"
                    ),
                    {
                        "u": c,
                        "s": sid,
                        "r": "user" if role == "User" else "assistant",
                        "c": content,
                    },
                )
        with user_session(c) as s:
            writer.consolidate_session(s, c, sid, llm, dt.date.today())
            new = [m for m in PgMemory().list_all(s, c) if m["id"] not in before]
        hits = []
        for e in chat["expect"]:
            ok = any(
                m["kind"] == e["kind"] and all(w in m["text"].lower() for w in e["words"])
                for m in new
            )
            hits.append(ok)
        expected += len(chat["expect"])
        matched_expect += sum(hits)
        stored += len(new)
        matched_stored += sum(
            any(all(w in m["text"].lower() for w in e["words"]) for e in chat["expect"])
            for m in new
        )
        leak = [x for x in chat["must_not_contain"] for m in new if x in m["text"]]
        clean += not leak
        good = all(hits) and not leak and (chat["expect"] or not new)
        res.cases.append(
            Case(
                f"writer:{i}",
                {"transcript": chat["transcript"]},
                {"expect": chat["expect"]},
                {"stored": [f"{m['kind']}/{m['topic']}: {m['text']}" for m in new], "leaked": leak},
                bool(good),
                error_type=None if good else "writer",
            )
        )
    details["writer"] = {"chats": len(data["chats"]), "expected": expected, "stored": stored}
    return {
        "writer_recall": matched_expect / max(expected, 1),
        "writer_precision": matched_stored / max(stored, 1),
        "writer_no_leak": clean / len(data["chats"]),
    }


def _chat(
    ctx: SuiteContext, data: dict[str, Any], llm: Any, res: SuiteResult, details: dict[str, Any]
) -> dict[str, float]:
    from finpilot.agents import chat_agent

    u = _user(ctx, "memory_chat", sample=True)
    fx = FXService(get_engine())  # stored + pinned rates only: no network in evals
    rows: list[dict[str, Any]] = []
    for q in data["chat_questions"]:
        t0 = time.perf_counter()
        r = chat_agent.respond(u, q, llm=llm, fx=fx)
        ms = int((time.perf_counter() - t0) * 1000)
        g = r.grounding or {}
        unverified = len(g.get("unverified") or [])
        grounded = r.route != "fallback" and unverified == 0 and r.route != "degraded"
        rows.append(
            {
                "route": r.route,
                "ms": ms,
                "grounded": grounded,
                "tools": [t.get("name") for t in r.tools],
            }
        )
        res.cases.append(
            Case(
                f"chat:{q[:60]}",
                {"question": q},
                {"grounded": True},
                {"answer": r.content, "route": r.route, "tools": rows[-1]["tools"], "ms": ms},
                grounded,
                error_type=None if grounded else f"chat_{r.route}",
            )
        )
    # scope guardrail: off-topic must get the fixed reply; in-scope must not (chat_questions above
    # plus the tricky in-scope ones)
    off_ok = 0
    for q in data["scope"]["off_topic"]:
        r = chat_agent.respond(u, q, llm=llm, fx=fx)
        good = r.route in ("out_of_scope", "smalltalk")
        off_ok += good
        res.cases.append(
            Case(
                f"scope_off:{q[:60]}",
                {"question": q},
                {"route": "out_of_scope"},
                {"route": r.route, "answer": r.content},
                good,
                error_type=None if good else "scope_answered_off_topic",
            )
        )
    wrong = sum(x["route"] in ("out_of_scope", "smalltalk") for x in rows)
    for q in data["scope"]["in_scope"]:
        r = chat_agent.respond(u, q, llm=llm, fx=fx)
        good = r.route not in ("out_of_scope", "smalltalk")
        wrong += not good
        res.cases.append(
            Case(
                f"scope_in:{q[:60]}",
                {"question": q},
                {"route": "not out_of_scope"},
                {"route": r.route, "answer": r.content},
                good,
                error_type=None if good else "scope_refused_in_scope",
            )
        )
    lat = [int(x["ms"]) for x in rows]
    details["chat"] = {"routes": dict(Counter(x["route"] for x in rows)), "questions": len(rows)}
    tool_rows = [x for x in rows if x["route"] in ("tools", "fallback")]
    return {
        "chat_grounded": sum(x["grounded"] for x in rows) / len(rows),
        "chat_fallback_rate": sum(x["route"] == "fallback" for x in rows) / len(rows),
        "chat_tool_use": sum(bool(x["tools"]) for x in tool_rows) / max(len(tool_rows), 1),
        "chat_p50_ms": float(np.percentile(lat, 50)),
        "chat_p95_ms": float(np.percentile(lat, 95)),
        "scope_refusal_recall": off_ok / len(data["scope"]["off_topic"]),
        "scope_false_refusal": wrong / (len(rows) + len(data["scope"]["in_scope"])),
    }
