"""Short-term chat memory: the last N turns of a session in Redis (`chat:{user}:{session}`, 24 h TTL)
plus a rolling summary of older turns (durable in `chat_sessions.summary`).

Redis is a cache here: on a miss the window is rebuilt from `chat_messages`. When the window
exceeds `max_turns` or the token budget, the oldest turns are folded into the summary by the fast
LLM (`prompts/chat/summarize.v1.md`); without Mesh the summary keeps a truncated transcript.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.llm.client import LLMClient, LLMError
from finpilot.llm.schemas import SummaryResponse
from finpilot.memory.config import config, tokens
from finpilot.redis_client import get_redis

log = logging.getLogger(__name__)
SUMMARY_MAX_CHARS = 1500


def key(uid: uuid.UUID | str, sid: uuid.UUID | str) -> str:
    return f"chat:{uid}:{sid}"


def _cfg() -> dict[str, Any]:
    return config()["short_term"]


def _cached(uid: uuid.UUID, sid: uuid.UUID) -> list[dict[str, str]] | None:
    try:
        raw = get_redis().get(key(uid, sid))
    except Exception:
        raw = None
    return list(json.loads(raw)) if raw else None


def _load(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> list[dict[str, str]]:
    cached = _cached(uid, sid)
    if cached is not None:
        return cached
    rows = s.execute(
        text("""SELECT role, content FROM chat_messages WHERE user_id = :u AND session_id = :s
                ORDER BY created_at DESC, (role = 'assistant') DESC LIMIT :n"""),
        {"u": uid, "s": sid, "n": int(_cfg()["max_turns"])},
    ).all()
    return [{"role": r_.role, "content": r_.content} for r_ in reversed(rows)]


def _save(uid: uuid.UUID, sid: uuid.UUID, turns: list[dict[str, str]]) -> None:
    try:
        get_redis().set(key(uid, sid), json.dumps(turns), ex=int(_cfg()["ttl_hours"]) * 3600)
    except Exception:
        log.debug("short-term memory: redis unavailable", exc_info=True)


def summary(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> str | None:
    return s.execute(
        text("SELECT summary FROM chat_sessions WHERE id = :s AND user_id = :u"),
        {"s": sid, "u": uid},
    ).scalar()


def window(s: Session, uid: uuid.UUID, sid: uuid.UUID) -> dict[str, Any]:
    """{'summary': str | None, 'turns': [{'role', 'content'}]} for the next prompt."""
    return {"summary": summary(s, uid, sid), "turns": _load(s, uid, sid)}


def append(
    s: Session,
    uid: uuid.UUID,
    sid: uuid.UUID,
    turns_: list[dict[str, str]],
    llm: LLMClient | None,
) -> None:
    """Add turns (already saved to chat_messages); fold the overflow (count or tokens) into the
    rolling summary. On a Redis miss the window is rebuilt from the table, which has them."""
    cached = _cached(uid, sid)
    turns = cached + turns_ if cached is not None else _load(s, uid, sid)
    cfg = _cfg()
    overflow: list[dict[str, str]] = []
    while len(turns) > int(cfg["max_turns"]) or (
        len(turns) > 2 and sum(tokens(t["content"]) for t in turns) > int(cfg["token_budget"])
    ):
        overflow.append(turns.pop(0))
    if overflow:
        fold(s, uid, sid, overflow, llm)
    _save(uid, sid, turns)


def fold(
    s: Session,
    uid: uuid.UUID,
    sid: uuid.UUID,
    dropped: list[dict[str, str]],
    llm: LLMClient | None,
) -> str:
    prev = summary(s, uid, sid) or ""
    transcript = "\n".join(f"{t['role']}: {t['content']}" for t in dropped)
    new: str | None = None
    if llm is not None and llm.available:
        try:
            out, _ = llm.chat_json(
                "chat/summarize",
                {"previous": prev, "transcript": transcript[:6000]},
                SummaryResponse,
                agent="chat",
                user_id=uid,
                session_id=str(sid),
            )
            new = out.summary
        except LLMError:
            log.info("rolling summary: LLM unavailable, truncating", exc_info=True)
    if new is None:  # degraded: keep the most recent part of a plain transcript
        new = (prev + "\n" + transcript).strip()[-SUMMARY_MAX_CHARS:]
    new = new[:SUMMARY_MAX_CHARS]
    s.execute(
        text("UPDATE chat_sessions SET summary = :m WHERE id = :s AND user_id = :u"),
        {"m": new, "s": sid, "u": uid},
    )
    return new


def forget(uid: uuid.UUID, sid: uuid.UUID) -> None:
    try:
        get_redis().delete(key(uid, sid))
    except Exception:
        log.debug("short-term memory: redis unavailable", exc_info=True)
