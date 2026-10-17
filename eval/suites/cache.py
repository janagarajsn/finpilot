"""Cache & latency suite (BUILD_PROMPT §Evaluation 7).

A synthetic user gets the product's "Load sample data" history (dates shifted to today, so
"this month" questions have data) and a pipeline-style prewarm of the dashboard chips. Then a
seeded 200-question session (eval/datasets/cache_questions.yml: intent weights + paraphrases; 40%
dashboard chips, 60% typed questions) runs through `answers.service.answer`, the same code the
API uses. Two expenses are added mid-session: the first answer per intent after each write must
not be a cache hit (invalidation). A second user then asks the same questions: none of its
answers may come from the first user's cache (isolation).

Metrics: hit rate (L2 + L3 over cacheable answers) and per layer, p50/p95 server-side latency by
layer, invalidation correctness, cross-user isolation, LLM cost saved by hits (full mode), router
accuracy on every labelled phrasing (offline: rules only; full: rules → embeddings → fast LLM).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import statistics
import uuid
from collections import Counter, defaultdict
from decimal import Decimal
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
from finpilot.agents import intent_router as router
from finpilot.answers import service
from finpilot.db.session import user_session
from finpilot.llm.client import CallRecord
from finpilot.transactions import service as txns

log = logging.getLogger(__name__)
SUITE = "cache"
BANK = Path(__file__).resolve().parents[1] / "datasets" / "cache_questions.yml"
N_QUESTIONS = 200
WRITES_AT = (70, 140)


def _user(ctx: SuiteContext, label: str) -> uuid.UUID:
    ds = generate(
        dataclasses.replace(
            get_persona("inr_salaried_emi_sip"), name=f"cache_{label.replace('-', '_')}"
        )
    )
    ld = loader.load(
        ds, ctx.run_tag, txns=ds.txns[:1]
    )  # an account + one old row; sample data adds the rest
    with user_session(ld.user_id) as s:
        s.execute(
            text("UPDATE users SET timezone = 'Asia/Kolkata', name = :n WHERE id = :u"),
            {"n": label, "u": ld.user_id},
        )
        load_sample(s, ld.user_id, None)
    return ld.user_id


def _session(rng: np.random.Generator, bank: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """[(intent_label, question or None for a chip, text)] in session order."""
    names = list(bank)
    w = np.array([bank[n]["weight"] for n in names], dtype=float)
    out = []
    for _ in range(N_QUESTIONS):
        intent = names[int(rng.choice(len(names), p=w / w.sum()))]
        qs = bank[intent]["questions"]
        q = qs[int(rng.integers(0, len(qs)))]
        chip = intent in service.CHIPS and rng.random() < 0.4
        out.append((intent, None if chip else q, q))
    return out


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    bank = yaml.safe_load(BANK.read_text(encoding="utf-8"))["intents"]
    rng = np.random.default_rng(8)
    llm = ctx.llm if ctx.mode == "full" else None
    records: list[CallRecord] = []
    if llm is not None:
        llm.add_sink(records.append)
    try:
        # ------------------------------------------------------------ router accuracy
        routed = []
        for intent, spec in bank.items():
            for q in spec["questions"]:
                r = router.route(q, llm)
                routed.append((intent, r.intent, r.via))
                res.cases.append(
                    Case(
                        case_id=f"route:{q[:60]}",
                        input={"question": q},
                        expected={"intent": intent},
                        actual={"intent": r.intent, "via": r.via},
                        passed=r.intent == intent,
                        score=float(r.intent == intent),
                        error_type=None if r.intent == intent else "misrouted",
                    )
                )
        # ------------------------------------------------------------ session
        a = _user(ctx, "cache-eval-a")
        with user_session(a) as s:
            service.prewarm(s, a, llm, None)
        session = _session(rng, bank)
        log_rows: list[dict[str, Any]] = []
        post_write_first: dict[str, bool] = {}
        invalidation: list[bool] = []
        for i, (label, question, _) in enumerate(session):
            if i in WRITES_AT:
                with user_session(a) as s:
                    txns.create(
                        s,
                        a,
                        txns.TxnInput(
                            "expense", Decimal("777"), dt.date.today(), f"EVAL WRITE {i}"
                        ),
                        None,
                        hash_suffix=f"evalwrite:{i}",
                    )
                post_write_first = {}
            with user_session(a) as s:
                out = service.answer(
                    s,
                    a,
                    question=question,
                    intent=None if question else label,
                    llm=llm,
                    source="eval",
                )
            if (
                i >= WRITES_AT[0]
                and out.intent not in post_write_first
                and out.intent not in ("afford_purchase",)
            ):
                post_write_first[out.intent] = True
                # the first answer per intent after a write must be freshly computed
                answered_before = any(r["intent"] == out.intent for r in log_rows)
                if answered_before and router.spec(out.intent).cacheable:
                    invalidation.append(out.cache_layer == "miss")
            log_rows.append(
                {
                    "intent": out.intent,
                    "layer": out.cache_layer,
                    "ms": out.latency_ms,
                    "chip": question is None,
                    "cacheable": router.spec(out.intent).cacheable,
                    "degraded": out.degraded,
                    "llm_intent": router.spec(out.intent).handler
                    in ("llm_from_facts", "free_form"),
                }
            )
        # ------------------------------------------------------------ isolation
        b = _user(ctx, "cache-eval-b")
        iso = []
        seen_b: set[str] = set()
        for label, question, _ in session[:60]:
            with user_session(b) as s:
                out = service.answer(
                    s,
                    b,
                    question=question,
                    intent=None if question else label,
                    llm=llm,
                    source="eval",
                )
            if out.intent not in seen_b and router.spec(out.intent).cacheable:
                iso.append(
                    out.cache_layer == "miss"
                )  # first ask of each intent by user B: never A's cache
            seen_b.add(out.intent)
    finally:
        if llm is not None:
            llm._sinks.remove(records.append)
        loader.cleanup(ctx.run_tag)

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    cacheable = [row for row in log_rows if row["cacheable"]]
    hits = [row for row in cacheable if row["layer"] in ("L2", "L3")]
    layers = Counter(row["layer"] for row in log_rows)
    lat: dict[str, list[int]] = defaultdict(list)
    for row in log_rows:
        lat[row["layer"]].append(row["ms"])
    add("hit_rate", len(hits) / max(len(cacheable), 1), "ratio", scope="overall", primary=True)
    for layer in ("L2", "L3", "miss", "none"):
        add(
            "layer_share",
            layers.get(layer, 0) / max(len(log_rows), 1),
            "ratio",
            scope="layer",
            layer=layer,
        )
        if lat.get(layer):
            add(
                "latency_p50_ms",
                float(np.percentile(lat[layer], 50)),
                "ms",
                scope="layer",
                layer=layer,
            )
            add(
                "latency_p95_ms",
                float(np.percentile(lat[layer], 95)),
                "ms",
                scope="layer",
                layer=layer,
            )
    cached_lat = lat.get("L2", []) + lat.get("L3", [])
    add(
        "cached_p95_ms",
        float(np.percentile(cached_lat, 95)) if cached_lat else 0.0,
        "ms",
        scope="overall",
        primary=True,
    )
    add(
        "miss_p95_ms",
        float(np.percentile(lat["miss"], 95)) if lat.get("miss") else 0.0,
        "ms",
        scope="overall",
        primary=True,
    )
    add(
        "invalidation_correct",
        sum(invalidation) / max(len(invalidation), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add("cross_user_isolation", sum(iso) / max(len(iso), 1), "ratio", scope="overall", primary=True)
    add(
        "router_accuracy",
        sum(t == p for t, p, _ in routed) / max(len(routed), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    for via, n in Counter(v for _, _, v in routed).items():
        add("router_share", n / len(routed), "ratio", scope="router", via=via)
    add(
        "degraded_share",
        sum(r["degraded"] for r in log_rows) / max(len(log_rows), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    if llm is not None:
        answer_calls = [r for r in records if r.agent == "answers" and r.cache_layer is None]
        per_miss = statistics.mean(r.cost_usd for r in answer_calls) if answer_calls else 0.0
        llm_hits = sum(1 for r in hits if r["llm_intent"])
        add("cost_saved_usd", llm_hits * per_miss, "usd", scope="overall", primary=True)
        add(
            "llm_calls_session",
            len([r for r in records if r.cache_layer is None]),
            "count",
            scope="overall",
            primary=True,
        )
    res.details = {
        "questions": len(log_rows),
        "layers": dict(layers),
        "writes_at": list(WRITES_AT),
        "invalidation_checks": len(invalidation),
        "isolation_checks": len(iso),
        "latency": {
            k: {"p50": float(np.percentile(v, 50)), "p95": float(np.percentile(v, 95)), "n": len(v)}
            for k, v in lat.items()
        },
        "router": {
            "total": len(routed),
            "correct": sum(t == p for t, p, _ in routed),
            "by_layer": dict(Counter(v for _, _, v in routed)),
        },
    }
    return res
