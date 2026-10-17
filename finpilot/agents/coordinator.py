"""Coordinator: the LangGraph pipeline that runs every agent for one user.

    route_intent ─┬─ refresh_insights → ingest → [categorize] → analyze → forecast → goals → advise
                  │                       → persist_trail → snapshot → [deliver] → END
                  ├─ ask_affordability → simulator (NL what-if, may interrupt for a clarification)
                  ├─ answer_from_cache → answer service (intent router + L2/L3 caches)
                  ├─ free_chat → chat agent (tool loop + grounding guard, chat session memory)
                  └─ explain → decision-trail lookup

* State is checkpointed in Redis after every node (thread `user:{id}:run:{run_id}`), and every
  node commits its own DB transaction, so a run that crashes (or a worker that dies) resumes from
  the failed node with `resume_run`.
* Each node writes an `agent_runs` row, updates Redis `run:{run_id}` progress (Pipeline Trace,
  SSE) and opens a Langfuse span inside the run's trace.
* Conditional edges: categorize is skipped when nothing is unresolved; deliver runs only for
  scheduled runs and sends the weekly digest when the user's slot has come. The node before a skip records why; skipped steps get `skipped` rows.
* Degraded mode: Mesh not configured at start, or any LLM failure during the run → rules-only
  categorization, template advice and narration, zero further LLM calls; the run is flagged
  `degraded` (and scored in Langfuse).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from finpilot.db.session import current_data_version, user_session
from finpilot.llm.client import LLMClient, LLMError
from finpilot.observability.langfuse import (
    agent_span,
    client,
    langfuse_enabled,
    score_current_trace,
)
from finpilot.redis_client import get_redis

log = logging.getLogger(__name__)
PIPELINE = [
    "ingest",
    "categorize",
    "analyze",
    "forecast",
    "goals",
    "advise",
    "persist_trail",
    "snapshot",
    "deliver",
]
PROGRESS_TTL = 24 * 3600


class RunState(TypedDict, total=False):
    user_id: str
    run_id: str
    trigger: str
    intent: str  # refresh_insights | ask_affordability | answer_from_cache | free_chat | explain
    payload: dict[str, Any]
    as_of: str | None
    degraded: bool
    degraded_reason: str | None
    plan: dict[str, str]  # step → skip reason
    results: dict[str, Any]  # step → summary
    forecast_id: str | None
    answer: dict[str, Any] | None


# ---------------------------------------------------------------------------- progress + trace
def progress_key(run_id: str) -> str:
    return f"run:{run_id}"


def _progress(run_id: str, user_id: str, update: Callable[[dict[str, Any]], None]) -> None:
    try:
        r = get_redis()
        raw = r.get(progress_key(run_id))
        doc = (
            json.loads(raw)
            if raw
            else {"run_id": run_id, "user_id": user_id, "status": "running", "steps": []}
        )
        update(doc)
        doc["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
        r.set(progress_key(run_id), json.dumps(doc, default=str), ex=PROGRESS_TTL)
    except Exception:  # progress is a convenience; Postgres agent_runs is the durable record
        log.debug("progress update failed", exc_info=True)


def read_progress(run_id: str) -> dict[str, Any] | None:
    try:
        raw = get_redis().get(progress_key(run_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None


@contextmanager
def _node_span(name: str) -> Iterator[None]:
    cm = nullcontext()
    if langfuse_enabled():
        try:
            cm = client().start_as_current_observation(name=f"node:{name}", as_type="span")  # type: ignore[assignment]
        except Exception:
            cm = nullcontext()
    with cm:
        yield


def _cfg(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable") or {})


def _as_of(state: RunState) -> dt.date | None:
    v = state.get("as_of")
    return dt.date.fromisoformat(v) if v else None


def _llm(state: RunState, c: dict[str, Any]) -> LLMClient | None:
    return None if state.get("degraded") else c.get("llm")


# ---------------------------------------------------------------------------- node wrapper
def step(
    name: str,
) -> Callable[
    [Callable[..., dict[str, Any]]], Callable[[RunState, RunnableConfig], dict[str, Any]]
]:
    """fn(state, cfg) -> {"summary": {...}, "status": "succeeded|degraded", **state_updates}."""

    def deco(
        fn: Callable[..., dict[str, Any]],
    ) -> Callable[[RunState, RunnableConfig], dict[str, Any]]:
        def node(state: RunState, config: RunnableConfig) -> dict[str, Any]:
            c = _cfg(config)
            uid, run_id = uuid.UUID(state["user_id"]), state["run_id"]
            with user_session(uid) as s:
                row = s.execute(
                    text("""INSERT INTO agent_runs (user_id, run_id, step, status, langfuse_trace_id)
                            VALUES (:u, :r, :step, 'running', :t) RETURNING id"""),
                    {"u": uid, "r": run_id, "step": name, "t": c.get("trace_id")},
                ).scalar_one()
            _progress(
                run_id,
                state["user_id"],
                lambda d: d["steps"].append(
                    {
                        "step": name,
                        "status": "running",
                        "started_at": dt.datetime.now(dt.UTC).isoformat(),
                    }
                ),
            )
            t0 = time.perf_counter()
            try:
                with _node_span(name):
                    if c.get("fail_at") == name:  # test hook: simulate a crash in this node
                        c.pop("fail_at", None)
                        raise RuntimeError(f"injected failure in {name}")
                    out = fn(state, c)
            except Exception as e:
                ms = int((time.perf_counter() - t0) * 1000)
                err = f"{type(e).__name__}: {e}"[:500]
                with user_session(uid) as s:
                    s.execute(
                        text("""UPDATE agent_runs SET status = 'failed', ended_at = now(), summary_json = CAST(:j AS jsonb)
                                      WHERE id = :id AND user_id = :u"""),
                        {
                            "id": row,
                            "u": uid,
                            "j": json.dumps({"error": err, "ms": ms}),
                        },
                    )
                _progress(
                    run_id,
                    state["user_id"],
                    lambda d: d["steps"][-1].update(status="failed", ms=ms, error=err[:300]),
                )
                raise
            ms = int((time.perf_counter() - t0) * 1000)
            summary = json.loads(
                json.dumps({**out.pop("summary", {}), "ms": ms}, default=str)
            )  # checkpoint-safe
            status = out.pop("status", "succeeded")
            with user_session(uid) as s:
                s.execute(
                    text("""UPDATE agent_runs SET status = :st, ended_at = now(), summary_json = CAST(:j AS jsonb)
                                  WHERE id = :id AND user_id = :u"""),
                    {"st": status, "id": row, "u": uid, "j": json.dumps(summary, default=str)},
                )
            _progress(
                run_id,
                state["user_id"],
                lambda d: d["steps"][-1].update(status=status, ms=ms, summary=summary),
            )
            results = dict(state.get("results") or {})
            results[name] = summary
            return {**out, "results": results}

        node.__name__ = f"node_{name}"
        return node

    return deco


def _degrade(reason: str) -> dict[str, Any]:
    return {"degraded": True, "degraded_reason": reason}


# ---------------------------------------------------------------------------- nodes
def route_intent(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    return {}


@step("ingest")
def ingest(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.recurring.materializer import materialize_user

    uid = uuid.UUID(state["user_id"])
    created = 0
    if state.get("as_of") is None:  # live runs catch up declared recurring items first
        created = materialize_user(uid, c.get("fx")).created
    with user_session(uid) as s:
        pending = s.execute(
            text("""SELECT count(*) FROM transactions WHERE user_id = :u AND category_id IS NULL
                    AND coalesce(cat_source, '') <> 'user' AND NOT needs_review"""),
            {"u": uid},
        ).scalar_one()
    plan = dict(state.get("plan") or {})
    if pending == 0:
        plan["categorize"] = "nothing to categorize"
    if state.get("trigger") != "schedule":
        plan["deliver"] = "digests are sent by the scheduler only"
    return {"summary": {"materialized": created, "uncategorized": pending}, "plan": plan}


@step("categorize")
def categorize(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.categorizer import Categorizer

    uid = uuid.UUID(state["user_id"])
    llm = _llm(state, c)
    with user_session(uid) as s:
        rep = Categorizer(llm, use_llm=llm is not None).run(s, uid)
    out: dict[str, Any] = {"summary": rep.as_dict()}
    if rep.degraded and llm is not None:
        out |= _degrade("categorizer: Mesh call failed")
        out["status"] = "degraded"
    elif llm is None:
        out["status"] = "degraded" if state.get("degraded") else "succeeded"
    return out


@step("analyze")
def analyze(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.analyzer import Analyzer, get_report

    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        rep = (
            Analyzer().run(s, uid, as_of=_as_of(state))
            if state.get("as_of")
            else get_report(s, uid, refresh=True)
        )
    kinds: dict[str, int] = {}
    for f in rep.findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    return {
        "summary": {
            "findings": len(rep.findings),
            "by_kind": kinds,
            "subscriptions": len(rep.subscriptions),
            "recurring": len(rep.recurring),
            "analysis_ms": rep.duration_ms,
        }
    }


@step("forecast")
def forecast(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.forecasting.service import run_forecast

    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        fc = run_forecast(s, uid, as_of=_as_of(state), fx=c.get("fx"))
    o = fc.outcome
    return {
        "summary": {
            "forecast_run_id": fc.id,
            "verdict": o.verdict,
            "end_p50": o.end_p50,
            "min_p10": o.min_p10,
            "p_below_buffer": o.p_below_buffer,
            "low_confidence": fc.low_confidence,
            "paths": fc.n_paths,
        },
        "forecast_id": fc.id,
    }


@step("goals")
def goals(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents import goal_tracker
    from finpilot.forecasting.service import ForecastResult

    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        row = s.execute(
            text("SELECT result_json FROM forecast_runs WHERE id = :i AND user_id = :u"),
            {"i": state["forecast_id"], "u": uid},
        ).scalar_one()
        fc = ForecastResult.model_validate(row)
        snaps = goal_tracker.snapshot(s, uid, fc)
    return {
        "summary": {
            "goals": len(snaps),
            "statuses": {x["name"]: x["status"] for x in snaps},
            "ahead": [f.data["name"] for f in goal_tracker.ahead_findings(fc)],
        }
    }


@step("advise")
def advise(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.advisor import Advisor

    uid = uuid.UUID(state["user_id"])
    llm = _llm(state, c)
    with user_session(uid) as s:
        run = Advisor(llm).run(s, uid, fx=c.get("fx"), as_of=_as_of(state), run_id=state["run_id"])
    out: dict[str, Any] = {
        "summary": {
            "recommendations": len(run.recommendations),
            "candidates": run.n_candidates,
            "generated_by": run.generated_by,
            "first_attempt_pass": run.guard.get("first_attempt_pass"),
            "retries": run.guard.get("retries"),
        }
    }
    if llm is not None and ["generation failed"] in (run.guard.get("problems") or []):
        out |= _degrade("advisor: Mesh call failed")
        out["status"] = "degraded"
    elif llm is None and state.get("degraded"):
        out["status"] = "degraded"
    return out


@step("persist_trail")
def persist_trail(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        recs, trails = s.execute(
            text("""SELECT count(*), count(t.id) FROM recommendations r LEFT JOIN decision_trail t ON t.recommendation_id = r.id
                    WHERE r.user_id = :u AND r.run_id = :run"""),
            {"u": uid, "run": state["run_id"]},
        ).one()
    if recs != trails:
        raise RuntimeError(f"{recs - trails} recommendations without a decision trail")
    return {"summary": {"recommendations": recs, "trails": trails}}


@step("snapshot")
def snapshot(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.analyzer import get_report
    from finpilot.answers.service import prewarm
    from finpilot.trail.narration import summarize_report

    uid = uuid.UUID(state["user_id"])
    llm = _llm(state, c)
    summary: dict[str, Any] = {}
    with user_session(uid) as s:
        if state.get("as_of") is None:
            summ = summarize_report(s, uid, get_report(s, uid), llm)
            summary["insights_summary"] = summ.get("generated_by")
            summary["prewarmed"] = prewarm(s, uid, llm, c.get("fx"))  # instant chips (L2)
        dv = current_data_version(uid)
        s.execute(
            text("""INSERT INTO insight_snapshots (user_id, kind, data_version, reporting_currency, as_of, payload_json)
                    SELECT :u, 'pipeline', :dv, reporting_currency, current_date, CAST(:p AS jsonb) FROM users WHERE id = :u
                    ON CONFLICT (user_id, kind) DO UPDATE SET data_version = EXCLUDED.data_version,
                      reporting_currency = EXCLUDED.reporting_currency, as_of = EXCLUDED.as_of,
                      payload_json = EXCLUDED.payload_json, created_at = now()"""),
            {
                "u": uid,
                "dv": dv,
                "p": json.dumps(
                    {
                        "run_id": state["run_id"],
                        "degraded": bool(state.get("degraded")),
                        "degraded_reason": state.get("degraded_reason"),
                        "results": state.get("results"),
                    },
                    default=str,
                ),
            },
        )
    summary["data_version"] = dv
    return {"summary": summary, "status": "degraded" if state.get("degraded") else "succeeded"}


@step("deliver")
def deliver(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    """Scheduled runs only: send the weekly digest if the user's slot has come (built from the
    data this run just refreshed). Idempotent per period; a degraded run says so in the digest."""
    from finpilot.digests.schedule import due_period
    from finpilot.digests.service import send_digest

    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        pref = (
            s.execute(
                text("""SELECT u.timezone, st.digest_enabled, st.digest_dow, st.digest_hour
                        FROM users u JOIN user_settings st ON st.user_id = u.id WHERE u.id = :u"""),
                {"u": uid},
            )
            .mappings()
            .first()
        )
    if pref is None or not pref["digest_enabled"]:
        return {"summary": {"digest": "disabled"}}
    pk = due_period(
        dt.datetime.now(dt.UTC), pref["timezone"], pref["digest_dow"], pref["digest_hour"]
    )
    if pk is None:
        return {"summary": {"digest": "not due"}}
    out = send_digest(
        uid,
        pk,
        llm=_llm(state, c),
        fx=c.get("fx"),
        degraded=bool(state.get("degraded")),
        run_id=state["run_id"],
    )
    return {"summary": {"digest": out["status"], "period": pk, "via": out.get("via")}}


@step("ask_affordability")
def ask_affordability(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents import simulator_graph
    from finpilot.recurring.service import user_today

    uid = uuid.UUID(state["user_id"])
    llm = _llm(state, c)
    if llm is None:
        return {
            "summary": {"status": "unavailable"},
            "status": "degraded",
            "answer": {
                "status": "unavailable",
                "message": "The assistant is unavailable; use the what-if form.",
            },
        }
    with user_session(uid) as s:
        ans = simulator_graph.ask(
            s, uid, state["payload"]["question"], llm, c.get("fx"), user_today(s, uid)
        )
    return {"summary": {"status": ans["status"], "thread_id": ans.get("thread_id")}, "answer": ans}


@step("explain")
def explain(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.advisor import get_trail

    uid = uuid.UUID(state["user_id"])
    with user_session(uid) as s:
        t = get_trail(s, uid, uuid.UUID(state["payload"]["recommendation_id"]))
    return {
        "summary": {"recommendation_id": str(t["id"]), "findings": len(t["findings"])},
        "answer": json.loads(json.dumps(t, default=str)),
    }


@step("answer_from_cache")
def answer_from_cache(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.answers import service as answers

    uid = uuid.UUID(state["user_id"])
    p = state["payload"]
    with user_session(uid) as s:
        a = answers.answer(
            s,
            uid,
            question=p.get("question"),
            intent=p.get("answer_intent"),
            llm=_llm(state, c),
            fx=c.get("fx"),
            source="pipeline",
        )
    return {
        "summary": {"intent": a.intent, "cache_layer": a.cache_layer, "ms": a.latency_ms},
        "answer": a.model_dump(mode="json"),
    }


@step("free_chat")
def free_chat(state: RunState, c: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents import chat_agent

    p = state["payload"]
    r = chat_agent.respond(  # opens its own transactions (question first, then the answer)
        uuid.UUID(state["user_id"]),
        p["question"],
        session_id=p.get("session_id"),
        llm=_llm(state, c),
        fx=c.get("fx"),
    )
    return {
        "summary": {"route": r.route, "session_id": r.session_id},
        "answer": json.loads(json.dumps(r.as_dict(), default=str)),
    }


# ---------------------------------------------------------------------------- graph
DIRECT = ("ask_affordability", "answer_from_cache", "free_chat", "explain")


def _after_route(state: RunState) -> str:
    intent = state.get("intent") or ""
    return intent if intent in DIRECT else "ingest"


def _after_ingest(state: RunState) -> str:
    return "analyze" if "categorize" in (state.get("plan") or {}) else "categorize"


def _after_snapshot(state: RunState) -> str:
    return END if "deliver" in (state.get("plan") or {}) else "deliver"


def build_graph(checkpointer: Any) -> Any:
    g = StateGraph(RunState)
    g.add_node("route_intent", route_intent)  # type: ignore[call-overload]
    for name, fn in (
        ("ingest", ingest),
        ("categorize", categorize),
        ("analyze", analyze),
        ("forecast", forecast),
        ("goals", goals),
        ("advise", advise),
        ("persist_trail", persist_trail),
        ("snapshot", snapshot),
        ("deliver", deliver),
        ("ask_affordability", ask_affordability),
        ("answer_from_cache", answer_from_cache),
        ("free_chat", free_chat),
        ("explain", explain),
    ):
        g.add_node(name, fn)  # type: ignore[call-overload,arg-type]
    g.add_edge(START, "route_intent")
    g.add_conditional_edges("route_intent", _after_route, ["ingest", *DIRECT])
    g.add_conditional_edges("ingest", _after_ingest, ["categorize", "analyze"])
    g.add_edge("categorize", "analyze")
    for a, b in (
        ("analyze", "forecast"),
        ("forecast", "goals"),
        ("goals", "advise"),
        ("advise", "persist_trail"),
        ("persist_trail", "snapshot"),
    ):
        g.add_edge(a, b)
    g.add_conditional_edges("snapshot", _after_snapshot, ["deliver", END])
    g.add_edge("deliver", END)
    for name in DIRECT:
        g.add_edge(name, END)
    return g.compile(checkpointer=checkpointer)


_graph: Any = None


def graph() -> Any:
    global _graph
    if _graph is None:
        from finpilot.agents.simulator_graph import checkpointer

        _graph = build_graph(checkpointer())
    return _graph


def thread_id(user_id: uuid.UUID | str, run_id: str) -> str:
    return f"user:{user_id}:run:{run_id}"


# ---------------------------------------------------------------------------- runs
class PipelineError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def create_run(user_id: uuid.UUID, trigger: str, s: Any = None) -> str:
    """Insert the run header (in the caller's transaction when `s` is given)."""

    def _ins(sess: Any) -> str:
        return str(
            sess.execute(
                text("""INSERT INTO pipeline_runs (user_id, trigger, data_version_start)
                    SELECT :u, :t, data_version FROM users WHERE id = :u RETURNING id"""),
                {"u": user_id, "t": trigger},
            ).scalar_one()
        )

    if s is not None:
        return _ins(s)
    with user_session(user_id) as sess:
        return _ins(sess)


def _config(
    user_id: uuid.UUID,
    run_id: str,
    llm: LLMClient | None,
    fx: Any,
    trace_id: str | None,
    fail_at: str | None = None,
) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id(user_id, run_id),
            "llm": llm,
            "fx": fx,
            "trace_id": trace_id,
            "fail_at": fail_at,
        }
    }


def _finish(
    user_id: uuid.UUID, run_id: str, final: RunState | None, error: str | None, trace_id: str | None
) -> dict[str, Any]:
    results = (final or {}).get("results") or {}
    plan = (final or {}).get("plan") or {}
    degraded = bool((final or {}).get("degraded"))
    with user_session(user_id) as s:
        if error is None and (final or {}).get("intent", "refresh_insights") == "refresh_insights":
            for name in PIPELINE:  # steps the graph routed around get a visible 'skipped' row
                if name not in results and name in plan:
                    s.execute(
                        text("""INSERT INTO agent_runs (user_id, run_id, step, status, ended_at, summary_json)
                                      VALUES (:u, :r, :step, 'skipped', now(), CAST(:j AS jsonb))"""),
                        {
                            "u": user_id,
                            "r": run_id,
                            "step": name,
                            "j": json.dumps({"reason": plan[name]}),
                        },
                    )
        s.execute(
            text("""UPDATE pipeline_runs SET status = :st, degraded = :deg, degraded_reason = :why, ended_at = now(),
                    data_version_end = (SELECT data_version FROM users WHERE id = :u), summary_json = CAST(:j AS jsonb),
                    error = :err, langfuse_trace_id = coalesce(:tid, langfuse_trace_id)
                    WHERE id = :r AND user_id = :u"""),
            {
                "st": "failed" if error else "succeeded",
                "deg": degraded,
                "why": (final or {}).get("degraded_reason"),
                "u": user_id,
                "r": run_id,
                "j": json.dumps(
                    {"results": results, "plan": plan, "answer": (final or {}).get("answer")},
                    default=str,
                ),
                "err": error,
                "tid": trace_id,
            },
        )
    _progress(
        run_id,
        str(user_id),
        lambda d: d.update(
            status="failed" if error else "succeeded", degraded=degraded, error=error, skipped=plan
        ),
    )
    return {
        "run_id": run_id,
        "status": "failed" if error else "succeeded",
        "degraded": degraded,
        "degraded_reason": (final or {}).get("degraded_reason"),
        "results": results,
        "plan": plan,
        "answer": (final or {}).get("answer"),
        "error": error,
    }


def run(
    user_id: uuid.UUID,
    run_id: str,
    *,
    trigger: str = "manual",
    intent: str = "refresh_insights",
    payload: dict[str, Any] | None = None,
    as_of: dt.date | None = None,
    llm: LLMClient | None = None,
    fx: Any = None,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Execute a created run to the end (or until it fails; then it can be resumed)."""
    degraded = llm is None or not llm.available
    init: RunState = {
        "user_id": str(user_id),
        "run_id": run_id,
        "trigger": trigger,
        "intent": intent,
        "payload": payload or {},
        "as_of": as_of.isoformat() if as_of else None,
        "degraded": degraded,
        "degraded_reason": "Mesh API not configured" if degraded else None,
        "plan": {},
        "results": {},
        "forecast_id": None,
        "answer": None,
    }
    _progress(
        run_id,
        str(user_id),
        lambda d: d.update(status="running", trigger=trigger, intent=intent, degraded=degraded),
    )
    with agent_span(
        "pipeline",
        user_id=user_id,
        session_id=f"run:{run_id}",
        metadata={"trigger": trigger, "intent": intent},
    ) as tid:
        try:
            final = graph().invoke(
                init, _config(user_id, run_id, None if degraded else llm, fx, tid, fail_at)
            )
        except LLMError as e:  # should not escape nodes, but never fail a run on Mesh alone
            log.warning("pipeline %s LLM error escaped a node", run_id, exc_info=True)
            return _finish(user_id, run_id, None, f"LLM: {e}", tid)
        except Exception as e:
            log.exception("pipeline %s failed", run_id)
            return _finish(user_id, run_id, None, f"{type(e).__name__}: {e}"[:500], tid)
        score_current_trace("degraded", 1.0 if final.get("degraded") else 0.0)
        return _finish(user_id, run_id, final, None, tid)


def resume_run(
    user_id: uuid.UUID, run_id: str, llm: LLMClient | None = None, fx: Any = None
) -> dict[str, Any]:
    """Continue a failed / interrupted run from its last checkpoint (the failed node runs again)."""
    snap = graph().get_state({"configurable": {"thread_id": thread_id(user_id, run_id)}})
    if snap is None or not snap.values:
        raise PipelineError("No checkpoint for this run.", 404)
    if not snap.next:
        raise PipelineError("This run already finished.", 409)
    with user_session(user_id) as s:
        s.execute(
            text(
                "UPDATE pipeline_runs SET status = 'running', error = NULL, ended_at = NULL WHERE id = :r AND user_id = :u"
            ),
            {"r": run_id, "u": user_id},
        )
    degraded = bool(snap.values.get("degraded")) or llm is None or not llm.available
    with agent_span(
        "pipeline", user_id=user_id, session_id=f"run:{run_id}", metadata={"trigger": "resume"}
    ) as tid:
        try:
            final = graph().invoke(
                None, _config(user_id, run_id, None if degraded else llm, fx, tid)
            )
        except Exception as e:
            log.exception("pipeline %s failed again on resume", run_id)
            return _finish(user_id, run_id, None, f"{type(e).__name__}: {e}"[:500], tid)
        return _finish(user_id, run_id, final, None, tid)


def list_runs(s: Any, user_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in s.execute(
            text("""SELECT id, trigger, status, degraded, degraded_reason, started_at, ended_at, error, langfuse_trace_id,
                       data_version_start, data_version_end
                FROM pipeline_runs WHERE user_id = :u ORDER BY started_at DESC LIMIT :n"""),
            {"u": user_id, "n": limit},
        ).mappings()
    ]


def get_run(s: Any, user_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("SELECT * FROM pipeline_runs WHERE id = :r AND user_id = :u"),
            {"r": run_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PipelineError("Run not found.", 404)
    out = dict(row)
    rows = [
        dict(r)
        for r in s.execute(
            text(
                "SELECT id, step, status, started_at, ended_at, summary_json FROM agent_runs WHERE run_id = :r AND user_id = :u ORDER BY id"
            ),
            {"r": run_id, "u": user_id},
        ).mappings()
    ]
    order = {name: i for i, name in enumerate(PIPELINE)}
    # pipeline order (skipped steps are written at the end of a run), retries after their failure
    out["steps"] = sorted(rows, key=lambda r: (order.get(r["step"], len(order)), r["id"]))
    out["progress"] = read_progress(str(run_id))
    return out
