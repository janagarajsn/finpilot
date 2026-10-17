"""Memory writer: what to remember long-term.

- Chat sessions: after a session has been idle for 15 min (the `memory_consolidation` tick), the
  fast LLM (`prompts/memory_writer/extract.v1.md`) extracts candidate memories from the turns since
  the last consolidation, seeing the user's existing memories so updates reuse their topic. Code
  then filters (confidence, blocked content, past expiry) and stores them through `PgMemory.write`
  (dedupe / supersede). Without Mesh nothing is extracted and the session stays pending.
- Recommendation accept/dismiss: a deterministic `decision` memory (templates/messages/memory.yml),
  one slot per candidate key, so a later change of mind supersedes it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.llm.client import LLMClient, LLMError
from finpilot.llm.schemas import MemoryExtraction
from finpilot.memory.config import config
from finpilot.memory.long_term import MemoryIn, PgMemory, WriteOutcome
from finpilot.templating import render_snippet

log = logging.getLogger(__name__)
PROMPT = "memory_writer/extract"


def _transcript(
    s: Session, uid: uuid.UUID, sid: uuid.UUID, since: dt.datetime | None
) -> tuple[str, dt.datetime | None]:
    """Turns after `since` as text, and the newest turn's timestamp (the next watermark)."""
    rows = s.execute(
        text("""SELECT role, content, created_at FROM chat_messages
                WHERE user_id = :u AND session_id = :s AND (CAST(:t AS timestamptz) IS NULL OR created_at > :t)
                ORDER BY created_at"""),
        {"u": uid, "s": sid, "t": since},
    ).all()
    txt = "\n".join(f"{'User' if r.role == 'user' else 'FinPilot'}: {r.content}" for r in rows)
    return txt[-int(config()["writer"]["max_transcript_chars"]) :], (
        rows[-1].created_at if rows else None
    )


def extract(
    s: Session, uid: uuid.UUID, transcript: str, llm: LLMClient, today: dt.date
) -> list[MemoryIn]:
    """LLM extraction + code-side filters. Raises LLMError when Mesh is unavailable."""
    mem = PgMemory(llm)
    existing = [
        {"kind": m["kind"], "topic": m["topic"], "text": m["text"]}
        for m in mem.list_all(s, uid)[:30]
    ]
    out, _ = llm.chat_json(
        PROMPT,
        {"today": today.isoformat(), "transcript": transcript, "existing": existing},
        MemoryExtraction,
        agent="memory_writer",
        user_id=uid,
    )
    cfg = config()
    keep: list[MemoryIn] = []
    for c in out.memories[: int(cfg["writer"]["max_candidates"])]:
        if c.confidence < float(cfg["long_term"]["min_confidence"]):
            continue
        expires = None
        if c.expires_on:
            try:
                d = dt.date.fromisoformat(c.expires_on)
            except ValueError:
                d = None
            if d is not None:
                if d < today:
                    continue  # already over: not worth remembering
                expires = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), dt.UTC)
        keep.append(
            MemoryIn(
                kind=c.kind,
                topic=c.topic,
                text=c.text,
                importance=c.importance,
                confidence=c.confidence,
                expires_at=expires,
            )
        )
    return keep


def consolidate_session(
    s: Session, uid: uuid.UUID, sid: uuid.UUID, llm: LLMClient | None, today: dt.date
) -> list[WriteOutcome] | None:
    """Extract and store memories from a session's new turns. None = skipped (no Mesh)."""
    row = s.execute(
        text(
            "SELECT consolidated_at, last_message_at FROM chat_sessions WHERE id = :s AND user_id = :u"
        ),
        {"s": sid, "u": uid},
    ).first()
    if row is None:
        return []
    transcript, newest = _transcript(s, uid, sid, row.consolidated_at)
    if not transcript.strip():
        return []
    if llm is None or not llm.available:
        return None
    try:
        cands = extract(s, uid, transcript, llm, today)
    except LLMError:
        log.info("memory writer: LLM unavailable, session %s stays pending", sid, exc_info=True)
        return None
    mem = PgMemory(llm)
    outcomes = []
    for c in cands:
        c.source, c.source_ref = "chat", f"chat:{sid}"
        outcomes.append(mem.write(s, uid, c))
    s.execute(
        text("UPDATE chat_sessions SET consolidated_at = :t WHERE id = :s AND user_id = :u"),
        {"t": newest, "s": sid, "u": uid},
    )
    return outcomes


def remember_decision(
    s: Session,
    uid: uuid.UUID,
    *,
    rec_id: str,
    title: str,
    action: str,
    candidate_key: str,
    reason: str | None,
    llm: LLMClient | None,
) -> WriteOutcome:
    status = "accepted" if action == "accept" else "dismissed"
    return PgMemory(llm).write(
        s,
        uid,
        MemoryIn(
            kind="decision",
            topic=f"advice_{candidate_key}"[:60],
            text=render_snippet("memory", f"decision.{status}", title=title, reason=reason),
            importance=0.7 if status == "dismissed" else 0.5,
            confidence=1.0,
            source="recommendation",
            source_ref=f"recommendation:{rec_id}",
        ),
    )


def idle_sessions(s: Session, limit: int = 200) -> list[dict[str, Any]]:
    """Sessions (all users; system session) idle ≥ idle_minutes with unconsolidated turns."""
    return [
        dict(r)
        for r in s.execute(
            text("""SELECT id, user_id FROM chat_sessions
                    WHERE (consolidated_at IS NULL OR consolidated_at < last_message_at)
                      AND last_message_at < now() - make_interval(mins => :m)
                    ORDER BY last_message_at LIMIT :n"""),
            {"m": int(config()["writer"]["idle_minutes"]), "n": limit},
        ).mappings()
    ]
