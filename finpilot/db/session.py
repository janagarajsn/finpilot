"""Engine/session factory. Every user-scoped unit of work runs in a transaction that sets
`app.user_id` with `SET LOCAL`, which Postgres RLS policies read. `SET LOCAL` is scoped to the
transaction, so the value never leaks to the next borrower of a pooled connection.

`users.data_version` is bumped by Postgres triggers in the same transaction as any write to
financial facts. After commit, `user_session` mirrors a changed value to Redis `dv:{user}`
(the durable truth stays in Postgres; the mirror is a fast cache-key ingredient).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from finpilot.config import get_settings

log = logging.getLogger(__name__)
_DV = text("SELECT data_version FROM users WHERE id = :u")


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(
        url or get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10
    )


@lru_cache
def _factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, autoflush=False)


def rowcount(result: object) -> int:
    """Rows affected by an UPDATE/DELETE (CursorResult.rowcount; typed helper)."""
    return int(getattr(result, "rowcount", 0) or 0)


def mirror_data_version(user_id: uuid.UUID | str, value: int) -> None:
    try:
        from finpilot.redis_client import get_redis

        get_redis().set(f"dv:{user_id}", value)
    except Exception:
        log.warning("could not mirror data_version for %s", user_id, exc_info=True)


@contextmanager
def user_session(user_id: uuid.UUID | str, url: str | None = None) -> Iterator[Session]:
    """Transaction bound to one tenant. Commits on success, rolls back on error."""
    uid = str(uuid.UUID(str(user_id)))  # validates format; never interpolate raw input
    s = _factory(url)()
    changed_to: int | None = None
    try:
        with s.begin():
            s.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": uid})
            before = s.execute(_DV, {"u": uid}).scalar()
            yield s
            after = s.execute(_DV, {"u": uid}).scalar()
            if after is not None and after != before:
                changed_to = int(after)
    finally:
        s.close()
    if changed_to is not None:
        mirror_data_version(uid, changed_to)


def current_data_version(user_id: uuid.UUID | str) -> int:
    """Redis mirror first, Postgres as the durable fallback (re-mirrors on miss)."""
    try:
        from finpilot.redis_client import get_redis

        v = get_redis().get(f"dv:{user_id}")
        if v is not None:
            return int(v)
    except Exception:
        log.debug("dv mirror unavailable", exc_info=True)
    with user_session(user_id) as s:
        value = int(s.execute(_DV, {"u": str(user_id)}).scalar() or 0)
    mirror_data_version(user_id, value)
    return value


@contextmanager
def system_session(reason: str, url: str | None = None) -> Iterator[Session]:
    """Admin-role transaction for system jobs (BYPASSRLS). `reason` is recorded for audit."""
    s = _factory(url or get_settings().database_admin_url)()
    try:
        with s.begin():
            s.execute(text("SELECT set_config('app.audit_reason', :r, true)"), {"r": reason[:200]})
            yield s
    finally:
        s.close()
