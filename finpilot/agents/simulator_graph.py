"""Natural-language what-if: parse (fast LLM via Mesh) → clarify missing slots with a LangGraph
`interrupt()` (state checkpointed in Redis, resumed by a later HTTP request) → deterministic
simulation → templated answer. The LLM only parses; every number comes from the simulator.

Thread ids are `sim-<user_id>-<random>`; resuming checks the owner prefix (tenancy).
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from functools import lru_cache
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy.orm import Session

from finpilot.categories.rules import thresholds
from finpilot.config import get_settings
from finpilot.forecasting import scenarios
from finpilot.forecasting.inputs import ForecastError
from finpilot.forecasting.service import simulate_scenario
from finpilot.forecasting.simulator import SimulationResult
from finpilot.llm.client import LLMClient, LLMError
from finpilot.llm.schemas import ParsedScenario
from finpilot.templating import render_snippet
from finpilot.trail.narration import narrate_simulation

PROMPT = "simulator_parser/parse"
REQUIRED = {
    "one_time_purchase": ["amount"],
    "recurring_cost": ["amount"],
    "savings_transfer": ["amount"],
    "cancel_subscription": ["merchant"],
    "category_cut": ["category", "pct"],
    "income_change": ["income"],
    "loan_or_installments": ["total", "n"],
}


class SimState(TypedDict, total=False):
    user_id: str
    question: str
    clarifications: list[dict[str, str]]
    parsed: dict[str, Any]
    scenario: dict[str, Any]
    result: dict[str, Any]
    answer: str
    narration: dict[str, Any]
    status: str  # done | unsupported | error
    message: str


# ---------------------------------------------------------------------------- pure helpers
def missing_slots(p: ParsedScenario) -> list[str]:
    out = []
    for slot in REQUIRED.get(p.intent, []):
        if (
            (slot in ("amount", "total") and not (p.amount and p.amount > 0))
            or (slot == "income" and p.pct is None and not p.amount)
            or (slot == "pct" and not (p.pct and p.pct > 0))
            or (slot in ("merchant", "category", "n") and not getattr(p, slot))
        ):
            out.append(slot)
    return out


def to_scenario(p: ParsedScenario, today: dt.date) -> dict[str, Any]:
    """Map parser output to a Scenario dict (validated by the Scenario schema afterwards)."""
    when: str | None = None
    if p.date:
        try:
            d = dt.date.fromisoformat(p.date)
            when = (
                d.isoformat() if d > today else None
            )  # "today"/past → tomorrow (simulator default)
        except ValueError:
            when = None
    amt = None if p.amount is None else f"{abs(p.amount):.2f}"
    label = (p.label or "").strip() or None
    t = p.intent
    if t == "one_time_purchase":
        return {
            "type": t,
            "amount": amt,
            "currency": p.currency,
            "date": when,
            **({"label": label} if label else {}),
        }
    if t == "recurring_cost":
        freq = p.frequency if p.frequency in ("weekly", "monthly", "yearly") else "monthly"
        return {
            "type": t,
            "amount": amt,
            "currency": p.currency,
            "frequency": freq,
            "start_date": when,
            "occurrences": p.occurrences,
            **({"label": label} if label else {}),
        }
    if t == "savings_transfer":
        return {
            "type": t,
            "amount": amt,
            "currency": p.currency,
            "frequency": "once" if p.frequency == "once" else "monthly",
            "start_date": when,
        }
    if t == "cancel_subscription":
        return {"type": t, "merchant": p.merchant, "from_date": when}
    if t == "category_cut":
        return {"type": t, "category": p.category, "pct": abs(p.pct or 0), "start_date": when}
    if t == "income_change":
        if p.pct is not None:
            return {"type": t, "pct": p.pct, "start_date": when}
        return {
            "type": t,
            "monthly_amount": f"{p.amount:.2f}",
            "currency": p.currency,
            "start_date": when,
        }
    if t == "loan_or_installments":
        return {
            "type": t,
            "total": amt,
            "n": p.n,
            "apr": p.apr or 0,
            "currency": p.currency,
            "start_date": when,
            **({"label": label} if label else {}),
        }
    raise ValueError(t)


def answer_text(r: SimulationResult) -> str:
    data = r.model_dump()  # Decimals stay Decimals for money()
    cur = r.currency

    def d(x: Any) -> str:
        return dt.date.fromisoformat(str(x)).strftime("%d %b")

    def pct(x: float) -> str:
        return f"{round(float(x) * 100)}%"

    def word(v: str) -> str:
        return render_snippet("simulation", f"verdict_word.{v}")

    kw = {"r": data, "cur": cur, "d": d, "pct": pct, "verdict_word": word}
    parts = [render_snippet("simulation", f"verdict.{r.result.verdict}", **kw)]
    if r.end_delta_mean < 0:
        parts.append(render_snippet("simulation", "effect", **kw))
    if r.baseline.verdict != "affordable":
        parts.append(render_snippet("simulation", "baseline_note", **kw))
    for lv in data["levers"]:
        parts.append(
            render_snippet(
                "simulation", f"lever.{lv['kind']}", t=lv["title_data"], o=lv["outcome"], **kw
            )
        )
    for fx in data["fx_used"]:
        parts.append(render_snippet("simulation", "fx_note", fx=fx, **kw))
    return " ".join(parts)


# ---------------------------------------------------------------------------- graph
def _cfg(config: RunnableConfig) -> dict[str, Any]:
    return dict(config.get("configurable") or {})


def _parse_node(state: SimState, config: RunnableConfig) -> dict[str, Any]:
    c = _cfg(config)
    llm: LLMClient = c["llm"]
    ctx_vars = c["prompt_context"]
    clar = list(state.get("clarifications") or [])
    max_rounds = int(thresholds()["simulator"]["max_clarifications"])
    while True:
        parsed, _ = llm.chat_json(
            PROMPT,
            {k: ctx_vars[k] for k in ("today", "currency", "categories", "subscriptions")}
            | {"question": state["question"], "clarifications": clar},
            ParsedScenario,
            agent="simulator_parser",
            user_id=state["user_id"],
            session_id=c.get("thread_id"),
            cache_scope=state["user_id"],
        )
        if parsed.intent == "unsupported":
            return {
                "parsed": parsed.model_dump(),
                "status": "unsupported",
                "message": render_snippet("simulation", "unsupported"),
                "clarifications": clar,
            }
        missing = missing_slots(parsed)
        if not missing:
            return {"parsed": parsed.model_dump(), "clarifications": clar}
        if len(clar) >= max_rounds:
            return {
                "parsed": parsed.model_dump(),
                "status": "error",
                "clarifications": clar,
                "message": render_snippet("simulation", "gave_up"),
            }
        slot = missing[0]
        examples = ", ".join(
            ctx_vars["subscription_names" if slot == "merchant" else "category_names"][:4]
        )
        q = render_snippet("simulation", f"clarify.{slot}", examples=examples)
        answer = interrupt({"question": q, "missing": missing})  # resumed with the user's reply
        clar.append({"q": q, "a": str(answer)[:300]})


def _simulate_node(state: SimState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("status") in ("unsupported", "error"):
        return {}
    c = _cfg(config)
    s: Session = c["db"]
    parsed = ParsedScenario.model_validate(state["parsed"])
    try:
        sc = scenarios.parse(to_scenario(parsed, c["today"]))
        res = simulate_scenario(s, uuid.UUID(state["user_id"]), sc, fx=c.get("fx"))
    except (ForecastError, ValidationError, ValueError) as e:
        msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
        return {"status": "error", "message": msg}
    answer, narration = narrate_simulation(
        c.get("llm"), res, state["question"], answer_text(res), uuid.UUID(state["user_id"])
    )
    return {
        "scenario": sc.model_dump(mode="json"),
        "result": res.model_dump(mode="json"),
        "answer": answer,
        "narration": narration,
        "status": "done",
    }


@lru_cache
def checkpointer() -> Any:
    from urllib.parse import urlsplit, urlunsplit

    from langgraph.checkpoint.redis import RedisSaver
    from redis import Redis

    st = get_settings()
    u = urlsplit(st.redis_url)
    db0 = urlunsplit((u.scheme, u.netloc, "/0", u.query, u.fragment))  # Redis Search: DB 0 only
    saver = RedisSaver(
        redis_client=Redis.from_url(db0, decode_responses=False),
        checkpoint_prefix=f"{st.langgraph_key_prefix}:checkpoint",
        checkpoint_write_prefix=f"{st.langgraph_key_prefix}:checkpoint_write",
        ttl={"default_ttl": 60},  # minutes: an unanswered clarification expires after an hour
    )
    saver.setup()
    return saver


@lru_cache
def graph() -> Any:
    g = StateGraph(SimState)
    g.add_node("parse", _parse_node)
    g.add_node("simulate", _simulate_node)
    g.add_edge(START, "parse")
    g.add_edge("parse", "simulate")
    g.add_edge("simulate", END)
    return g.compile(checkpointer=checkpointer())


class AskError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _prompt_context(s: Session, user_id: uuid.UUID, today: dt.date) -> dict[str, Any]:
    from sqlalchemy import text

    cur = s.execute(
        text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    cats = [
        r[0]
        for r in s.execute(
            text("""SELECT name FROM categories c WHERE (c.user_id = :u OR c.user_id IS NULL) AND NOT c.archived
                AND c.flow_type = 'expense' AND c.id NOT IN (SELECT category_id FROM hidden_categories WHERE user_id = :u)
                ORDER BY name"""),
            {"u": user_id},
        )
    ]
    subs = [
        r[0].title()
        for r in s.execute(
            text("""SELECT merchant_norm FROM detected_recurrences WHERE user_id = :u AND status = 'active'
                AND kind = 'expense' AND NOT dismissed ORDER BY amount_typical DESC LIMIT 20"""),
            {"u": user_id},
        )
    ]
    return {
        "today": today.isoformat(),
        "currency": cur,
        "categories": ", ".join(cats) or "none",
        "subscriptions": ", ".join(subs) or "none",
        "category_names": cats,
        "subscription_names": subs,
    }


def _run(
    s: Session,
    user_id: uuid.UUID,
    thread_id: str,
    payload: Any,
    llm: LLMClient,
    fx: Any,
    today: dt.date,
) -> dict[str, Any]:
    config = {
        "configurable": {
            "thread_id": thread_id,
            "db": s,
            "llm": llm,
            "fx": fx,
            "today": today,
            "prompt_context": _prompt_context(s, user_id, today),
        }
    }
    try:
        out = graph().invoke(payload, config)
    except LLMError as e:
        raise AskError("The assistant is unavailable right now; use the form instead.", 503) from e
    if out.get("__interrupt__"):
        intr = out["__interrupt__"][0].value
        return {
            "status": "needs_input",
            "thread_id": thread_id,
            "question": intr["question"],
            "missing": intr["missing"],
        }
    return {
        "status": out.get("status", "error"),
        "thread_id": thread_id,
        "answer": out.get("answer"),
        "message": out.get("message"),
        "narration": out.get("narration"),
        "scenario": out.get("scenario"),
        "result": out.get("result"),
        "clarifications": out.get("clarifications", []),
    }


def ask(
    s: Session, user_id: uuid.UUID, question: str, llm: LLMClient, fx: Any, today: dt.date
) -> dict[str, Any]:
    question = " ".join(question.split())[:500]
    if len(question) < 3:
        raise AskError("Ask a question, for example: can I afford a ₹80,000 trip next month?", 422)
    if not llm.available:
        raise AskError("The assistant is unavailable right now; use the form instead.", 503)
    thread_id = f"sim-{user_id}-{secrets.token_hex(8)}"
    return _run(
        s,
        user_id,
        thread_id,
        {"user_id": str(user_id), "question": question, "clarifications": []},
        llm,
        fx,
        today,
    )


def resume(
    s: Session,
    user_id: uuid.UUID,
    thread_id: str,
    answer: str,
    llm: LLMClient,
    fx: Any,
    today: dt.date,
) -> dict[str, Any]:
    if not thread_id.startswith(f"sim-{user_id}-"):
        raise AskError("Conversation not found.", 404)
    snap = graph().get_state({"configurable": {"thread_id": thread_id}})
    if not snap or not snap.next:
        raise AskError("Conversation not found or already finished.", 404)
    return _run(
        s, user_id, thread_id, Command(resume=" ".join(answer.split())[:300]), llm, fx, today
    )
