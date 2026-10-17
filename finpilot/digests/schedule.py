"""When is a user's digest due? Pure functions (no I/O) so the tick logic is testable.

Each user picks a weekday (0 = Sunday … 6 = Saturday) and an hour in THEIR timezone. The most
recent such local slot defines the period (`period_key` = the slot's local date). A digest is due
from the slot for `DUE_WINDOW` (catch-up after downtime or a busy worker), and only once per
period: the UNIQUE (user_id, period_key) row makes repeated ticks harmless.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

DUE_WINDOW = dt.timedelta(hours=24)


def _tz(name: str | None) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def last_slot(now_utc: dt.datetime, tz: str | None, dow: int, hour: int) -> dt.datetime:
    """The latest local slot (weekday `dow`, 0 = Sunday, at `hour`:00) at or before now."""
    z = _tz(tz)
    local = now_utc.astimezone(z)
    want = (dow - 1) % 7  # Python: Monday = 0
    days_back = (local.weekday() - want) % 7
    d = local.date() - dt.timedelta(days=days_back)
    slot = dt.datetime.combine(d, dt.time(hour), z)
    if slot > local:  # today is the weekday but the hour has not come yet
        slot = dt.datetime.combine(d - dt.timedelta(days=7), dt.time(hour), z)
    return slot


def due_period(
    now_utc: dt.datetime, tz: str | None, dow: int, hour: int, window: dt.timedelta = DUE_WINDOW
) -> str | None:
    """period_key if a digest is due right now (within `window` after the slot), else None."""
    slot = last_slot(now_utc, tz, dow, hour)
    return slot.date().isoformat() if now_utc - slot < window else None


def period_label(period_key: str) -> str:
    start = dt.date.fromisoformat(period_key)
    prev = start - dt.timedelta(days=7)
    return f"{prev.strftime('%d %b')} – {start.strftime('%d %b %Y')}"
