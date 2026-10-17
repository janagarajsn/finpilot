"""Answers to the questions users ask most, with layered caching (BUILD_PROMPT § CACHING).

    question ─► router (rules → embeddings → fast LLM) ─► intent + period
             ─► L2  cache:ans:{user}:{intent}:{period}:{dv}:{currency}:{locale}:{version}:{model}[:{day}][:fx{asof}]
             ─► L3  semantic_cache (same user/intent/period/dv/currency/day, cosine ≥ 0.92)
             ─► compute under a single-flight lock: deterministic facts → template, or LLM phrasing
                under the grounding guard (template fallback) → store in L2 (+ L3 for typed questions)

L2 is invalidated by construction: any financial write bumps data_version, so old keys are never
read again (no scanning; they expire by TTL). Degraded answers (Mesh failed or unavailable for an
LLM intent) and user-amount answers (afford_purchase) are never cached.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from functools import lru_cache
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.agents import intent_router as router
from finpilot.answers import facts as facts_mod
from finpilot.config import get_settings
from finpilot.db.session import current_data_version
from finpilot.llm.client import LLMClient
from finpilot.llm.prompt_registry import get_registry
from finpilot.llm.schemas import NarrationResponse
from finpilot.money import get_currency_registry
from finpilot.observability.langfuse import agent_span, score_current_trace
from finpilot.recurring.service import user_today
from finpilot.redis_client import get_redis
from finpilot.templating import render_snippet
from finpilot.trail.grounding import FactSet, guarded_json

log = logging.getLogger(__name__)
PROMPT = "answers/answer"
CHIPS = ["spending_trend", "where_to_save", "where_overspending", "upcoming_bills"]
LOCK_WAIT_S = 8.0


class AnswerOut(BaseModel):
    answer: str
    intent: str
    period: str
    period_label: str
    cache_layer: str  # miss | L2 | L3 | none
    route: str
    generated_by: str  # template | llm | simulator
    created_at: str
    age_seconds: int
    latency_ms: int
    degraded: bool = False
    grounding: dict[str, Any] | None = None
    facts: dict[str, Any] | None = None
    simulation: dict[str, Any] | None = None
    suggest_chat: bool = False  # the question names a category/merchant: Chat can be precise


@lru_cache
def template_version() -> str:
    path = get_settings().templates_dir / "messages" / "answers.yml"
    return "t" + hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def fx_asof(s: Session, user_id: uuid.UUID, currency: str) -> str | None:
    """Newest stored rate date for the user's foreign currencies (None if all in one currency)."""
    curs = [
        r[0]
        for r in s.execute(
            text(
                "SELECT DISTINCT currency FROM transactions WHERE user_id = :u AND currency <> :c"
            ),
            {"u": user_id, "c": currency},
        )
    ]
    if not curs:
        return None
    base = get_currency_registry().base
    quotes = sorted({c for c in [*curs, currency] if c != base})
    d = s.execute(
        text(
            "SELECT max(rate_date) FROM fx_rates WHERE scope = 'real' AND quote_currency = ANY(:q) AND rate_date <= current_date"
        ),
        {"q": quotes},
    ).scalar()
    return d.isoformat() if d else "none"


def l2_key(
    user_id: uuid.UUID | str,
    intent: str,
    period: str,
    data_version: int,
    currency: str,
    locale: str,
    version: str,
    model: str,
    day: str | None,
    fx: str | None,
) -> str:
    key = f"cache:ans:{user_id}:{intent}:{period}:{data_version}:{currency}:{locale}:{version}:{model}"
    if day:
        key += f":{day}"
    if fx:
        key += f":fx{fx}"
    return key


def _json(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _render(intent: str, f: dict[str, Any]) -> str:
    if not f.get("has_data", True):
        return render_snippet("answers", "no_data", **f, cur=f["currency"])
    return render_snippet("answers", intent, **f, cur=f["currency"])


def _compute(
    s: Session,
    uid: uuid.UUID,
    sp: router.IntentSpec,
    period: str,
    question: str,
    llm: LLMClient | None,
    fx: Any,
) -> dict[str, Any]:
    f = facts_mod.build(s, uid, sp.name, period, fx)
    template = _render(sp.name, f)
    if sp.handler == "deterministic_template" or not f.get("has_data", True):
        return {
            "answer": template,
            "facts": f,
            "generated_by": "template",
            "degraded": False,
            "grounding": None,
        }
    if llm is None or not llm.available:
        return {
            "answer": template,
            "facts": f,
            "generated_by": "template",
            "degraded": True,
            "grounding": {"reason": "Mesh unavailable"},
        }
    cur = f["currency"]
    fs = FactSet().add_obj(f, cur)
    out, guard = guarded_json(
        llm,
        PROMPT,
        {
            "intent": sp.name,
            "question": question[:500],
            "period_label": f["period_label"],
            "currency": cur,
            "symbol": get_currency_registry().get(cur).symbol,
            "facts": json.dumps(f, default=str, ensure_ascii=False, indent=1),
        },
        NarrationResponse,
        facts=fs,
        texts=lambda x: [x.answer],
        validate=lambda x: _borrowed_words(x.answer, question, f),
        fallback=lambda: NarrationResponse(answer=template),
        agent="answers",
        user_id=uid,
        cache_scope=str(uid),
    )
    failed = guard.fallback_used and ["generation failed"] in guard.problems
    return {
        "answer": out.answer,
        "facts": f,
        "generated_by": "template" if guard.fallback_used else "llm",
        "degraded": failed,
        "grounding": guard.as_dict(),
    }


def _borrowed_words(answer: str, question: str | None, facts: dict[str, Any]) -> list[str]:
    """A canonical answer covers the whole picture: it must not label its figures with a specific
    thing the user asked about that the facts do not contain ("₹73,185 on cricket gear")."""
    known = router.content_words(json.dumps(facts, default=str, ensure_ascii=False))
    borrowed = sorted(
        (router.specific_words(question or "") - known) & router.content_words(answer)
    )
    return [render_snippet("answers", "borrowed_words", words=borrowed)] if borrowed else []


# ---------------------------------------------------------------------------- L3 semantic cache
def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def semantic_lookup(
    s: Session,
    uid: uuid.UUID,
    vector: list[float],
    intent: str,
    period: str,
    dv: int,
    currency: str,
    bucket: str,
) -> dict[str, Any] | None:
    thr = float(router.config()["settings"]["semantic_cache_threshold"])
    row = s.execute(
        text("""SELECT id, answer_json, 1 - (embedding <=> CAST(:v AS vector)) AS sim FROM semantic_cache
                WHERE user_id = :u AND intent = :i AND period = :p AND data_version = :dv
                  AND reporting_currency = :c AND as_of_bucket = :b
                ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"""),
        {
            "v": _vec(vector),
            "u": uid,
            "i": intent,
            "p": period,
            "dv": dv,
            "c": currency,
            "b": bucket,
        },
    ).first()
    if row is None or float(row.sim) < thr:
        return None
    s.execute(
        text("UPDATE semantic_cache SET hits = hits + 1 WHERE id = :i AND user_id = :u"),
        {"i": row.id, "u": uid},
    )
    return dict(row.answer_json) | {"similarity": round(float(row.sim), 4)}


def semantic_store(
    s: Session,
    uid: uuid.UUID,
    question: str,
    vector: list[float],
    intent: str,
    period: str,
    dv: int,
    currency: str,
    bucket: str,
    payload: dict[str, Any],
) -> None:
    s.execute(
        text("""INSERT INTO semantic_cache (user_id, intent, period, question_text, embedding, answer_json, data_version,
                reporting_currency, as_of_bucket) VALUES (:u, :i, :p, :q, CAST(:v AS vector), CAST(:a AS jsonb), :dv, :c, :b)"""),
        {
            "u": uid,
            "i": intent,
            "p": period,
            "q": question[:500],
            "v": _vec(vector),
            "a": json.dumps(payload, default=str),
            "dv": dv,
            "c": currency,
            "b": bucket,
        },
    )


# ---------------------------------------------------------------------------- main entry
def answer(
    s: Session,
    user_id: uuid.UUID,
    *,
    question: str | None = None,
    intent: str | None = None,
    period: str | None = None,
    llm: LLMClient | None = None,
    fx: Any = None,
    refresh: bool = False,
    source: str = "api",
    route: router.Route | None = None,
) -> AnswerOut:
    """`route`: an already-routed question (the chat agent routes first to pick its path)."""
    t0 = time.perf_counter()
    with agent_span("answer", user_id=user_id, metadata={"intent": intent, "source": source}):
        out = _answer(s, user_id, question, intent, period, llm, fx, refresh, t0, route)
        score_current_trace("cache_hit", 1.0 if out.cache_layer in ("L2", "L3") else 0.0)
    if source != "prewarm":
        s.execute(
            text("""INSERT INTO answer_log (user_id, intent, period, route, cache_layer, generated_by, latency_ms, degraded)
                    VALUES (:u, :i, :p, :r, :l, :g, :ms, :d)"""),
            {
                "u": user_id,
                "i": out.intent,
                "p": out.period,
                "r": out.route,
                "l": out.cache_layer,
                "g": out.generated_by,
                "ms": out.latency_ms,
                "d": out.degraded,
            },
        )
    return out


def _answer(
    s: Session,
    uid: uuid.UUID,
    question: str | None,
    intent: str | None,
    period: str | None,
    llm: LLMClient | None,
    fx: Any,
    refresh: bool,
    t0: float,
    route: router.Route | None = None,
) -> AnswerOut:
    if route is not None:
        rt = dataclasses.replace(route)
        if period:
            rt.period = period
    elif intent:
        sp = router.spec(intent)
        rt = router.Route(
            intent, period or router.config()["settings"]["default_period"], "chip", 1.0, sp
        )
        question = question or (sp.examples[0] if sp.examples else intent)
    else:
        if not question or not question.strip():
            raise ValueError("Ask a question or pick an intent.")
        rt = router.route(question, llm, uid)
        if period:
            rt.period = period
    sp = rt.spec
    user = (
        s.execute(text("SELECT reporting_currency, timezone FROM users WHERE id = :u"), {"u": uid})
        .mappings()
        .one()
    )
    cur = user["reporting_currency"]
    today = user_today(s, uid)

    def done(payload: dict[str, Any], layer: str) -> AnswerOut:
        created = payload.get("created_at") or dt.datetime.now(dt.UTC).isoformat()
        age = int((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(created)).total_seconds())
        f = payload.get("facts") or {}
        return AnswerOut(
            answer=payload["answer"],
            intent=rt.intent,
            period=rt.period,
            period_label=f.get("period_label") or rt.period,
            cache_layer=layer,
            route=rt.via,
            generated_by=payload.get("generated_by", "template"),
            created_at=created,
            age_seconds=max(age, 0),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            degraded=bool(payload.get("degraded")),
            grounding=payload.get("grounding"),
            facts=f or None,
            simulation=payload.get("simulation"),
        )

    if sp.handler in ("decline", "smalltalk"):  # scope guardrail: fixed reply, no LLM, no cache
        key = "out_of_scope" if sp.handler == "decline" else "smalltalk"
        return done({"answer": render_snippet("chat", key), "generated_by": "template"}, "none")

    if sp.handler == "simulator":  # user-specified amounts: never cached
        from finpilot.agents import simulator_graph

        if llm is None or not llm.available:
            return done(
                {
                    "answer": render_snippet("simulation", "unavailable"),
                    "generated_by": "template",
                    "degraded": True,
                },
                "none",
            )
        res = simulator_graph.ask(s, uid, question or "", llm, fx, today)
        text_ = res.get("answer") or res.get("question") or res.get("message") or ""
        return done(
            {"answer": text_, "generated_by": "simulator", "simulation": _json(res)}, "none"
        )

    dv = current_data_version(uid)
    locale = get_currency_registry().get(cur).locale
    llm_intent = sp.handler in ("llm_from_facts", "free_form")
    version = (
        f"{get_registry().get(PROMPT).meta.name}v{get_registry().get(PROMPT).meta.version}"
        if llm_intent
        else template_version()
    )
    model = get_settings().mesh_model_fast if llm_intent else "none"
    bucket = today.isoformat() if sp.time_relative else "any"
    key = l2_key(
        uid,
        sp.name,
        rt.period,
        dv,
        cur,
        locale,
        version,
        model,
        bucket if sp.time_relative else None,
        fx_asof(s, uid, cur),
    )
    r = get_redis()
    use_l2 = sp.cacheable and sp.handler != "free_form"  # free-form text varies: only L3 applies
    if use_l2 and not refresh:
        raw = r.get(key)
        if raw:
            return done(json.loads(raw), "L2")
    if (
        sp.cacheable
        and not refresh
        and question
        and rt.vector is None
        and llm is not None
        and llm.available
    ):
        try:  # chips/rules matches have no vector yet; L3 needs one only for typed questions
            if rt.via != "chip" and llm.embeddings_available():
                rt.vector = llm.embed([question], agent="answers", user_id=uid)[0]
        except Exception:
            rt.vector = None
    if sp.cacheable and not refresh and rt.vector is not None and rt.via != "chip":
        hit = semantic_lookup(s, uid, rt.vector, sp.name, rt.period, dv, cur, bucket)
        if hit:
            return done(hit, "L3")
    lock = f"cache:lock:{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    got = bool(r.set(lock, "1", nx=True, ex=60))
    try:
        if not got and use_l2:  # someone else is computing the same answer: wait for it
            deadline = time.monotonic() + LOCK_WAIT_S
            while time.monotonic() < deadline:
                time.sleep(0.05)
                raw = r.get(key)
                if raw:
                    return done(json.loads(raw), "L2")
        payload = _compute(s, uid, sp, rt.period, question or "", llm, fx)
        payload = _json(payload | {"created_at": dt.datetime.now(dt.UTC).isoformat()})
        if sp.cacheable and not payload["degraded"]:
            if use_l2:
                r.set(key, json.dumps(payload), ex=sp.ttl)
            if rt.vector is not None and rt.via != "chip":
                semantic_store(
                    s, uid, question or "", rt.vector, sp.name, rt.period, dv, cur, bucket, payload
                )
        return done(payload, "miss")
    finally:
        if got:
            r.delete(lock)


def prewarm(
    s: Session, user_id: uuid.UUID, llm: LLMClient | None, fx: Any = None
) -> dict[str, str]:
    """Compute the chip intents into L2 (and insight_snapshots) so the dashboard answers instantly."""
    out: dict[str, str] = {}
    snap: dict[str, Any] = {}
    for intent in CHIPS:
        try:
            a = answer(s, user_id, intent=intent, llm=llm, fx=fx, source="prewarm")
            out[intent] = (
                a.cache_layer
                if a.cache_layer == "L2"
                else ("cached" if not a.degraded else "degraded")
            )
            snap[intent] = {
                "answer": a.answer,
                "generated_by": a.generated_by,
                "created_at": a.created_at,
            }
        except Exception:
            log.warning("prewarm %s failed for %s", intent, user_id, exc_info=True)
            out[intent] = "error"
    s.execute(
        text("""INSERT INTO insight_snapshots (user_id, kind, data_version, reporting_currency, as_of, payload_json)
                SELECT :u, 'answers', data_version, reporting_currency, current_date, CAST(:p AS jsonb) FROM users WHERE id = :u
                ON CONFLICT (user_id, kind) DO UPDATE SET data_version = EXCLUDED.data_version,
                  reporting_currency = EXCLUDED.reporting_currency, as_of = EXCLUDED.as_of,
                  payload_json = EXCLUDED.payload_json, created_at = now()"""),
        {"u": user_id, "p": json.dumps(snap, default=str)},
    )
    return out


def is_stale(s: Session, user_id: uuid.UUID) -> bool:
    """No pre-warmed answers for today's data_version + reporting currency."""
    fresh = s.execute(
        text("""SELECT 1 FROM insight_snapshots i JOIN users u ON u.id = i.user_id
                WHERE i.user_id = :u AND i.kind = 'answers' AND i.data_version = u.data_version
                  AND i.reporting_currency = u.reporting_currency AND i.as_of = current_date"""),
        {"u": user_id},
    ).first()
    return fresh is None


def layer_stats(
    s: Session, user_id: uuid.UUID | None = None, since_days: int = 30
) -> list[dict[str, Any]]:
    """Hit counts and p50/p95 latency by cache layer (the user's own rows under RLS)."""
    rows = s.execute(
        text("""SELECT cache_layer, count(*) AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
                FROM answer_log WHERE created_at > now() - make_interval(days => :d)
                  AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))
                GROUP BY cache_layer ORDER BY cache_layer"""),
        {"d": since_days, "u": user_id},
    ).mappings()
    return [dict(r) for r in rows]
