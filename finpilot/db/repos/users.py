"""User profile and settings repository. Every function takes `user_id` and filters by it
(defense in depth on top of RLS)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

PROFILE_COLS = (
    "id, email::text AS email, name, reporting_currency, timezone, role, data_version, "
    "onboarded_at, created_at"
)
SETTINGS_COLS = (
    "digest_enabled, digest_channel, digest_dow, digest_hour, risk_alert_enabled, cash_buffer, "
    "telegram_chat_id IS NOT NULL AS telegram_linked"
)
UPDATABLE_PROFILE = {"name", "reporting_currency", "timezone"}
UPDATABLE_SETTINGS = {
    "digest_enabled",
    "digest_channel",
    "digest_dow",
    "digest_hour",
    "risk_alert_enabled",
    "cash_buffer",
}


def get_profile(s: Session, user_id: uuid.UUID) -> dict[str, Any]:
    sql = f"SELECT {PROFILE_COLS} FROM users WHERE id = :u"  # noqa: S608 — constant columns
    return dict(s.execute(text(sql), {"u": user_id}).mappings().one())


def update_profile(s: Session, user_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    changes = {k: v for k, v in changes.items() if k in UPDATABLE_PROFILE and v is not None}
    if changes:
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        sql = f"UPDATE users SET {sets} WHERE id = :u"  # noqa: S608 — keys allowlisted above
        s.execute(text(sql), {**changes, "u": user_id})
    return get_profile(s, user_id)


def mark_onboarded(s: Session, user_id: uuid.UUID) -> None:
    s.execute(
        text("UPDATE users SET onboarded_at = coalesce(onboarded_at, now()) WHERE id = :u"),
        {"u": user_id},
    )


def get_settings_row(s: Session, user_id: uuid.UUID) -> dict[str, Any]:
    sql = f"SELECT {SETTINGS_COLS} FROM user_settings WHERE user_id = :u"  # noqa: S608 — constant
    return dict(s.execute(text(sql), {"u": user_id}).mappings().one())


def update_settings_row(s: Session, user_id: uuid.UUID, changes: dict[str, Any]) -> dict[str, Any]:
    changes = {k: v for k, v in changes.items() if k in UPDATABLE_SETTINGS}
    if "cash_buffer" in changes and changes["cash_buffer"] is not None:
        changes["cash_buffer"] = Decimal(str(changes["cash_buffer"]))
    if changes:
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        sql = f"UPDATE user_settings SET {sets} WHERE user_id = :u"  # noqa: S608 — keys allowlisted
        s.execute(text(sql), {**changes, "u": user_id})
    return get_settings_row(s, user_id)


def list_accounts(s: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT id, name, type, currency, opening_balance, archived FROM accounts "
            "WHERE user_id = :u ORDER BY created_at"
        ),
        {"u": user_id},
    ).mappings()
    return [dict(r) for r in rows]
