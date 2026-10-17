"""Your data: a full JSON export of everything a user owns, and account deletion.

Export = every user-owned table (RLS-scoped session, so only this user's rows), minus secrets
(password/token hashes) and bulky derived data (vectors, caches, logs). Deletion is one
`DELETE FROM users` (every user table cascades), after re-checking the password; Redis keys for
the user are removed and an anonymous audit event (hashed id) is kept.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.auth.service import ClientInfo, InvalidCredentials, _ip, revoke_access_before
from finpilot.db.session import system_session, user_session
from finpilot.observability.masking import hash_user_id
from finpilot.redis_client import get_redis
from finpilot.security.passwords import verify_password

log = logging.getLogger(__name__)
EXPORT_TABLES = [
    "user_settings",
    "accounts",
    "categories",
    "merchant_memory",
    "transactions",
    "recurring_rules",
    "detected_recurrences",
    "import_batches",
    "goals",
    "goal_snapshots",
    "recommendations",
    "decision_trail",
    "analysis_findings",
    "memories",
    "chat_sessions",
    "chat_messages",
    "digests",
    "alerts",
    "auth_events",
]
DROP_COLUMNS = {"password_hash", "token_hash", "embedding", "tsv"}
REDIS_PATTERNS = [
    "chat:{u}:*",
    "cache:ans:{u}:*",
    "dv:{u}",
    "lock:digest:{u}:*",
    "pipeline:pending:{u}",
]


def _plain(v: Any) -> Any:
    if isinstance(v, uuid.UUID | Decimal | dt.date | dt.datetime):
        return str(v)
    return v


def export(s: Session, uid: uuid.UUID) -> dict[str, Any]:
    present = {
        r[0]
        for r in s.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
    }
    profile = (
        s.execute(
            text("""SELECT id, email, name, reporting_currency, timezone, role, created_at, onboarded_at
                    FROM users WHERE id = :u"""),
            {"u": uid},
        )
        .mappings()
        .one()
    )
    out: dict[str, Any] = {
        "format": "finpilot-export/1",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "profile": {k: _plain(v) for k, v in profile.items()},
    }
    for table in EXPORT_TABLES:
        if table not in present:
            continue
        rows = s.execute(
            text(f"SELECT * FROM {table} WHERE user_id = :u ORDER BY 1"),  # noqa: S608 — fixed names
            {"u": uid},
        ).mappings()
        out[table] = [{k: _plain(v) for k, v in r.items() if k not in DROP_COLUMNS} for r in rows]
    return out


def export_json(s: Session, uid: uuid.UUID) -> str:
    return json.dumps(export(s, uid), default=str, ensure_ascii=False, indent=1)


def delete_account(uid: uuid.UUID, password: str, client: ClientInfo | None = None) -> None:
    client = client or ClientInfo()
    with user_session(uid) as s:
        h = s.execute(
            text("SELECT password_hash FROM users WHERE id = :u"), {"u": uid}
        ).scalar_one()
        if not verify_password(h, password):
            raise InvalidCredentials("Password is incorrect.")
        s.execute(
            text("DELETE FROM users WHERE id = :u"), {"u": uid}
        )  # cascades to every user table
    revoke_access_before(uid)
    try:
        r = get_redis()
        for pat in REDIS_PATTERNS:
            keys = list(r.scan_iter(pat.format(u=uid), count=500))
            if keys:
                r.delete(*keys)
    except Exception:
        log.warning("redis cleanup after account deletion failed", exc_info=True)
    with system_session("account_deleted") as s:  # anonymous audit trail (the user row is gone)
        s.execute(
            text("""INSERT INTO auth_events (user_id, kind, ip, user_agent, detail)
                    VALUES (NULL, 'account_deleted', CAST(:ip AS inet), :ua, CAST(:d AS jsonb))"""),
            {
                "ip": _ip(client.ip),
                "ua": (client.user_agent or "")[:300],
                "d": json.dumps({"user": hash_user_id(uid)}),
            },
        )
