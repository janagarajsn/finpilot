"""Memory jobs: consolidate idle chat sessions into long-term memory (tick every 15 min) and
remember recommendation decisions (enqueued on accept/dismiss)."""

from __future__ import annotations

import logging
import uuid

from finpilot.db.session import system_session, user_session
from finpilot.tasks.celery_app import app
from finpilot.tasks.locks import redis_lock

log = logging.getLogger(__name__)


def _llm():
    from finpilot.llm.client import get_llm

    return get_llm()


@app.task(name="finpilot.tasks.memory_consolidation")
def memory_consolidation() -> dict:
    from finpilot.memory import writer
    from finpilot.recurring.service import user_today

    with redis_lock("beat:memory_consolidation", ttl_s=840) as got:
        if not got:
            return {"skipped": 1}
        with system_session("memory_consolidation") as s:
            pending = writer.idle_sessions(s)
        done = skipped = written = 0
        llm = _llm()
        for row in pending:
            uid, sid = uuid.UUID(str(row["user_id"])), uuid.UUID(str(row["id"]))
            try:
                with user_session(uid) as s:
                    out = writer.consolidate_session(s, uid, sid, llm, user_today(s, uid))
            except Exception:
                log.warning("memory consolidation failed for session %s", sid, exc_info=True)
                continue
            if out is None:
                skipped += 1
            else:
                done += 1
                written += sum(1 for o in out if o.action != "blocked")
        return {"sessions": done, "skipped_no_llm": skipped, "memories": written}


@app.task(name="finpilot.tasks.remember_decision")
def remember_decision(
    user_id: str, rec_id: str, title: str, action: str, candidate_key: str, reason: str | None
) -> str:
    from finpilot.memory import writer

    uid = uuid.UUID(user_id)
    with user_session(uid) as s:
        out = writer.remember_decision(
            s,
            uid,
            rec_id=rec_id,
            title=title,
            action=action,
            candidate_key=candidate_key,
            reason=reason,
            llm=_llm(),
        )
    return out.action


def schedule_remember_decision(
    user_id: uuid.UUID, rec_id: str, title: str, action: str, candidate_key: str, reason: str | None
) -> None:
    try:
        remember_decision.delay(str(user_id), rec_id, title, action, candidate_key, reason)
    except Exception:  # broker down: the decision itself is stored on the recommendation
        log.warning("could not enqueue decision memory for %s", rec_id, exc_info=True)
