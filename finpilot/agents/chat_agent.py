"""Chat agent: one user message → one VERIFIED assistant message (BUILD_PROMPT §8).

Paths, cheapest first:
1. a pending what-if clarification in this session → resume the simulator graph
2. "can I afford …" → the NL what-if simulator (its own interruptible graph); never cached
3. a canonical question the intent router recognises with rules or embeddings → the answer
   service (L2/L3 caches, deterministic facts, grounded phrasing): instant when warm
4. anything else → a Mesh tool-calling loop (≤ 6 model turns) over config/chat_tools.yml; tool
   results go back to the model inside <data> and into a FactSet; the final text must pass the
   grounding guard (≤ 2 retries naming the unverified numbers) or the user gets deterministic
   headlines of the tool results instead. Tool-agent answers are not cached (they often involve
   user-specified amounts and conversation context).
Without Mesh: canonical questions still work (templates); the rest gets a clear notice.

Progress events ("Looking at your Dining Out spending…") are emitted while tools run; the final
text is emitted only after verification (unverified text is never streamed).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.agents import chat_tools as tools
from finpilot.agents import intent_router as router
from finpilot.db.session import rowcount, user_session
from finpilot.llm.client import LLMClient, LLMError
from finpilot.memory import short_term
from finpilot.memory.retrieval import recall
from finpilot.money import get_currency_registry
from finpilot.observability.langfuse import agent_span, score_current_trace
from finpilot.recurring.service import user_today
from finpilot.templating import render_snippet
from finpilot.trail.grounding import FactSet, GroundingReport, check, extract
from finpilot.trail.scope import violations

log = logging.getLogger(__name__)
PROMPT = "chat/agent"
Emit = Callable[[dict[str, Any]], None]


class ChatError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Reply:
    session_id: str
    message_id: str
    content: str
    route: str  # cache | simulator | tools | fallback | degraded | out_of_scope | smalltalk
    cache_layer: str | None = None
    intent: str | None = None
    grounding: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    needs_input: bool = False
    latency_ms: int = 0
    trace_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "content": self.content,
            "route": self.route,
            "cache_layer": self.cache_layer,
            "intent": self.intent,
            "grounding": self.grounding,
            "tools": self.tools,
            "needs_input": self.needs_input,
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------- sessions
def _session(s: Session, uid: uuid.UUID, sid: str | None, first: str) -> uuid.UUID:
    if sid:
        try:
            u = uuid.UUID(str(sid))
        except ValueError as e:
            raise ChatError("Conversation not found.", 404) from e
        found = s.execute(
            text("SELECT id FROM chat_sessions WHERE id = :s AND user_id = :u"), {"s": u, "u": uid}
        ).scalar()
        if found is None:
            raise ChatError("Conversation not found.", 404)
        return u
    return uuid.UUID(
        str(
            s.execute(
                text("INSERT INTO chat_sessions (user_id, title) VALUES (:u, :t) RETURNING id"),
                {"u": uid, "t": first[:60]},
            ).scalar_one()
        )
    )


def _save(s: Session, uid: uuid.UUID, sid: uuid.UUID, role: str, content: str, **kw: Any) -> str:
    mid = s.execute(
        text("""INSERT INTO chat_messages (user_id, session_id, role, content, route, tools_json, grounding_json,
                                           cache_layer, langfuse_trace_id, latency_ms)
                VALUES (:u, :s, :r, :c, :route, CAST(:tools AS jsonb), CAST(:g AS jsonb), :cl, :tid, :ms)
                RETURNING id"""),
        {
            "u": uid,
            "s": sid,
            "r": role,
            "c": content[:8000],
            "route": kw.get("route"),
            "tools": json.dumps(kw["tools"], default=str) if kw.get("tools") is not None else None,
            "g": json.dumps(kw["grounding"], default=str) if kw.get("grounding") else None,
            "cl": kw.get("cache_layer"),
            "tid": kw.get("trace_id"),
            "ms": kw.get("latency_ms"),
        },
    ).scalar_one()
    s.execute(
        text("UPDATE chat_sessions SET last_message_at = now() WHERE id = :s AND user_id = :u"),
        {"s": sid, "u": uid},
    )
    return str(mid)


def _pending_simulation(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> str | None:
    """The what-if thread waiting for this session's next message (durable: last assistant row)."""
    row = s.execute(
        text("""SELECT route, tools_json FROM chat_messages
                WHERE user_id = :u AND session_id = :s AND role = 'assistant'
                ORDER BY created_at DESC LIMIT 1"""),
        {"u": uid, "s": sid},
    ).first()
    if row is None or row.route != "simulator" or not row.tools_json:
        return None
    sim = (row.tools_json or [{}])[0]
    return sim.get("thread_id") if sim.get("status") == "needs_input" else None


# ---------------------------------------------------------------------------- paths
_words = router.content_words


def names_entity(s: Session, uid: uuid.UUID, question: str) -> bool:
    """Is the question about something specific? It names one of the user's categories or
    frequent merchants, or has a content word the canonical questions never use ("cricket
    gear"). Then a canonical cached answer (about the whole picture) would miss the point, and
    its phrasing could label the overall total with the user's words."""
    generic = _words(" ".join(router.config()["settings"]["generic_entity_words"]))
    asked = _words(question) - generic
    if not asked:
        return False
    if router.specific_words(question):
        return True
    names = s.execute(
        text("""SELECT name FROM categories WHERE (user_id = :u OR user_id IS NULL) AND NOT archived
                UNION ALL
                (SELECT merchant_norm FROM transactions WHERE user_id = :u AND merchant_norm IS NOT NULL
                   AND date > current_date - 180 GROUP BY merchant_norm ORDER BY count(*) DESC LIMIT 100)"""),
        {"u": uid},
    ).scalars()
    return any(asked & (_words(n) - generic) for n in names)


def _simulator(
    s: Session,
    uid: uuid.UUID,
    message: str,
    llm: LLMClient,
    fx: Any,
    today: dt.date,
    thread: str | None,
) -> tuple[str, list[dict[str, Any]], bool, dict[str, Any] | None]:
    from finpilot.agents import simulator_graph

    try:
        res = (
            simulator_graph.resume(s, uid, thread, message, llm, fx, today)
            if thread
            else simulator_graph.ask(s, uid, message, llm, fx, today)
        )
    except simulator_graph.AskError as e:
        return str(e), [{"name": "simulator", "status": "error"}], False, None
    content = res.get("answer") or res.get("question") or res.get("message") or ""
    trace = [{"name": "simulator", "status": res["status"], "thread_id": res.get("thread_id")}]
    narration = res.get("narration") or {}
    return content, trace, res["status"] == "needs_input", narration.get("grounding")


def _fallback(results: list[tuple[str, dict[str, Any]]]) -> str:
    lines = []
    for name, r in results:
        if r.get("error"):
            continue
        try:
            h = render_snippet("chat", f"headline.{name}", r=r)
        except Exception:
            log.debug("chat fallback headline failed for %s", name, exc_info=True)
            h = ""
        if h and h not in lines:
            lines.append(h)
    if not lines:
        return render_snippet("chat", "fallback_empty")
    return " ".join([render_snippet("chat", "fallback_intro"), *lines])


def _tool_loop(
    s: Session,
    uid: uuid.UUID,
    sid: uuid.UUID,
    message: str,
    history: dict[str, Any],
    llm: LLMClient,
    fx: Any,
    today: dt.date,
    currency: str,
    emit: Emit,
) -> tuple[str, str, GroundingReport, list[dict[str, Any]]]:
    cfg = tools.config()["settings"]
    memories = recall(s, uid, message, llm)
    facts = FactSet()
    facts.add_date(today)
    for m in extract(message):  # the user's own figures may be repeated back
        if m.kind == "money" and m.value is not None and m.currency:
            facts.add_money(m.value, m.currency)
        if m.value is not None:
            facts.add_number(m.value)
        if m.date and m.date[0] and m.date[1]:
            facts.add_date(dt.date(m.date[0], m.date[1], m.date[2] or 1))
    for mem in memories:  # what the user told us before (shown in the prompt)
        facts.add_literal(mem["text"])
    variables = {
        "today": today.isoformat(),
        "currency": currency,
        "symbol": get_currency_registry().get(currency).symbol,
        "memories": memories,
        "summary": history.get("summary"),
    }
    convo: list[dict[str, Any]] = [
        *({"role": t["role"], "content": t["content"]} for t in history["turns"]),
        {"role": "user", "content": message},
    ]
    ctx = tools.ToolContext(s, uid, llm, fx, today, currency)
    specs = tools.openai_tools()
    trace: list[dict[str, Any]] = []
    results: list[tuple[str, dict[str, Any]]] = []
    retries = 0
    rep = GroundingReport()
    for _ in range(int(cfg["max_iterations"])):
        res = llm.chat_tools(
            PROMPT, variables, specs, agent="chat", history=convo, user_id=uid, session_id=str(sid)
        )
        if any(tc["name"] == "decline_off_topic" for tc in res.tool_calls or []):
            # scope guardrail, last layer: the model recognised an off-topic request
            trace.append({"name": "decline_off_topic", "args": {}, "ok": True, "ms": 0})
            return render_snippet("chat", "out_of_scope"), "out_of_scope", rep, trace
        if res.tool_calls:
            convo.append(res.raw_message or {"role": "assistant", "content": res.text})
            for tc in res.tool_calls:
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                try:
                    emit(
                        {
                            "type": "progress",
                            "text": render_snippet("chat", f"progress.{tc['name']}", a=args),
                        }
                    )
                except Exception:
                    emit({"type": "progress", "text": render_snippet("chat", "progress.route")})
                t0 = time.perf_counter()
                out = tools.run_tool(ctx, tc["name"], args)
                trace.append(
                    {
                        "name": tc["name"],
                        "args": args,
                        "ok": "error" not in out,
                        "ms": int((time.perf_counter() - t0) * 1000),
                    }
                )
                results.append((tc["name"], out))
                facts.add_obj(out, currency)
                body = json.dumps(out, default=str)[: int(cfg["max_tool_output_chars"])]
                convo.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": f"<data>{body}</data>"}
                )
            continue
        draft = (res.text or "").strip()
        emit({"type": "progress", "text": render_snippet("chat", "progress.verify")})
        rep = check([draft], facts)
        directives = violations(draft)  # advice scope: no investment/tax/legal directives
        if directives:
            score_current_trace("advice_scope_blocked", 1.0, comment=directives[0][0])
        if rep.pii:
            score_current_trace("pii_leak_blocked", 1.0, comment=", ".join(rep.pii))
        if draft and rep.passed and not directives:
            return draft, "tools", rep, trace
        if retries >= int(cfg["max_guard_retries"]):
            break
        retries += 1
        problems = [
            *(f"unverified number: {u['text']}" for u in rep.unverified),
            *(f"remove the {e.lower().replace('_', ' ')}" for e in rep.pii),
            *(msg for _, msg in directives),
        ] or ["empty answer"]
        convo += [
            {"role": "assistant", "content": draft or "(empty)"},
            {"role": "user", "content": render_snippet("chat", "retry", problems=problems)},
        ]
    return _fallback(results), "fallback", rep, trace


# ---------------------------------------------------------------------------- entry point
def respond(
    user_id: uuid.UUID,
    message: str,
    *,
    session_id: str | None = None,
    llm: LLMClient | None,
    fx: Any = None,
    emit: Emit | None = None,
) -> Reply:
    t0 = time.perf_counter()
    say: Emit = emit or (lambda _e: None)
    message = " ".join((message or "").split())[:2000]
    if len(message) < 2:
        raise ChatError("Type a question.", 422)
    with user_session(user_id) as s:  # tx 1: the question is stored even if answering fails
        sid = _session(s, user_id, session_id, message)
        history = short_term.window(s, user_id, sid)
        pending = _pending_simulation(s, user_id, sid)
        _save(s, user_id, sid, "user", message)
    say({"type": "session", "session_id": str(sid)})
    live = llm is not None and llm.available
    with (
        user_session(user_id) as s,
        agent_span("chat", user_id=user_id, session_id=str(sid)) as trace_id,
    ):
        today = user_today(s, user_id)
        currency = s.execute(
            text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
        say({"type": "progress", "text": render_snippet("chat", "progress.route")})
        rt = router.route(message, llm if live else None, user_id)
        reply = Reply(str(sid), "", "", "degraded", intent=rt.intent)
        rules_intent = router.by_rules(router.normalize(message))
        if pending and (rules_intent in (None, "afford_purchase")):
            if live:
                assert llm is not None
                say({"type": "progress", "text": render_snippet("chat", "progress.simulator")})
                reply.content, reply.tools, reply.needs_input, g = _simulator(
                    s, user_id, message, llm, fx, today, pending
                )
                reply.route, reply.grounding, reply.intent = "simulator", g, "afford_purchase"
            else:
                reply.content = render_snippet("simulation", "unavailable")
        elif rt.spec.handler in ("decline", "smalltalk"):
            # scope guardrail: FinPilot only talks about the user's money (no LLM call)
            reply.route = "out_of_scope" if rt.spec.handler == "decline" else "smalltalk"
            reply.content = render_snippet("chat", reply.route)
        elif rt.intent == "afford_purchase":
            if live:
                assert llm is not None
                say({"type": "progress", "text": render_snippet("chat", "progress.simulator")})
                reply.content, reply.tools, reply.needs_input, g = _simulator(
                    s, user_id, message, llm, fx, today, None
                )
                reply.route, reply.grounding = "simulator", g
            else:
                reply.content = render_snippet("simulation", "unavailable")
        elif (
            rt.via in ("rules", "embedding")
            and rt.spec.cacheable
            and rt.spec.handler != "free_form"
            and not (live and names_entity(s, user_id, message))
        ):
            from finpilot.answers import service as answers

            say({"type": "progress", "text": render_snippet("chat", "progress.cache")})
            a = answers.answer(
                s, user_id, question=message, llm=llm, fx=fx, source="chat", route=rt
            )
            reply.content, reply.route, reply.cache_layer = a.answer, "cache", a.cache_layer
            reply.grounding = a.grounding
        elif live:
            assert llm is not None
            try:
                content, route, rep, trace = _tool_loop(
                    s, user_id, sid, message, history, llm, fx, today, currency, say
                )
                reply.content, reply.route, reply.tools = content, route, trace
                reply.grounding = rep.as_dict() | {"fallback_used": route == "fallback"}
                score_current_trace("grounding_pass", 1.0 if route == "tools" else 0.0)
            except LLMError:
                log.info("chat: Mesh unavailable mid-answer", exc_info=True)
                reply.content = render_snippet("chat", "unavailable")
        else:
            reply.content = render_snippet("chat", "unavailable")
        if reply.route == "degraded":
            score_current_trace("degraded", 1.0)
        reply.latency_ms = int((time.perf_counter() - t0) * 1000)
        reply.trace_id = trace_id
        reply.message_id = _save(
            s,
            user_id,
            sid,
            "assistant",
            reply.content,
            route=reply.route,
            tools=reply.tools,
            grounding=reply.grounding,
            cache_layer=reply.cache_layer,
            trace_id=trace_id,
            latency_ms=reply.latency_ms,
        )
        short_term.append(
            s,
            user_id,
            sid,
            [{"role": "user", "content": message}, {"role": "assistant", "content": reply.content}],
            llm if live else None,
        )
    say({"type": "message", **reply.as_dict()})
    return reply


# ---------------------------------------------------------------------------- reads
def list_sessions(s: Session, uid: uuid.UUID, limit: int = 30) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in s.execute(
            text("""SELECT id, title, started_at, last_message_at,
                           (SELECT count(*) FROM chat_messages m WHERE m.session_id = c.id) AS messages
                    FROM chat_sessions c WHERE user_id = :u ORDER BY last_message_at DESC LIMIT :n"""),
            {"u": uid, "n": limit},
        ).mappings()
    ]


def get_session(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> dict[str, Any]:
    head = (
        s.execute(
            text(
                "SELECT id, title, started_at, last_message_at FROM chat_sessions WHERE id = :s AND user_id = :u"
            ),
            {"s": sid, "u": uid},
        )
        .mappings()
        .first()
    )
    if head is None:
        raise ChatError("Conversation not found.", 404)
    msgs = [
        dict(r)
        for r in s.execute(
            text("""SELECT id, role, content, route, cache_layer, feedback, tools_json, grounding_json,
                           latency_ms, created_at
                    FROM chat_messages WHERE session_id = :s AND user_id = :u
                    ORDER BY created_at, (role = 'assistant')"""),
            {"s": sid, "u": uid},
        ).mappings()
    ]
    return dict(head) | {"messages": msgs}


def delete_session(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> None:
    n = rowcount(
        s.execute(
            text("DELETE FROM chat_sessions WHERE id = :s AND user_id = :u"), {"s": sid, "u": uid}
        )
    )
    if not n:
        raise ChatError("Conversation not found.", 404)
    short_term.forget(uid, sid)


def feedback(s: Session, uid: uuid.UUID, mid: uuid.UUID, value: str | None) -> dict[str, Any]:
    if value not in ("up", "down", None):
        raise ChatError("Feedback must be up or down.", 422)
    row = s.execute(
        text("""UPDATE chat_messages SET feedback = :v WHERE id = :m AND user_id = :u AND role = 'assistant'
                RETURNING id, langfuse_trace_id"""),
        {"v": value, "m": mid, "u": uid},
    ).first()
    if row is None:
        raise ChatError("Message not found.", 404)
    if row.langfuse_trace_id and value:
        from finpilot.observability.langfuse import client, langfuse_enabled

        if langfuse_enabled():
            try:
                client().create_score(
                    trace_id=row.langfuse_trace_id,
                    name="user_feedback",
                    value=1.0 if value == "up" else 0.0,
                )
            except Exception:
                log.debug("chat feedback score failed", exc_info=True)
    return {"id": str(row.id), "feedback": value}
