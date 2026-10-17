"""User actions on DETECTED recurring series: list, promote to a declared rule, dismiss, and
rate subscription usage. Every action clears the analysis snapshot (derived rows don't bump
data_version)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.agents.analyzer import invalidate
from finpilot.db.session import rowcount
from finpilot.recurring import service as rec
from finpilot.recurring.rules import _add_months
from finpilot.recurring.service import RecurringError

_SELECT = """
SELECT d.*, c.name AS category_name, c.color AS category_color
FROM detected_recurrences d LEFT JOIN categories c ON c.id = d.category_id
WHERE d.user_id = :u"""
MONTH_STEPS = {"monthly": 1, "quarterly": 3, "yearly": 12}
DAY_STEPS = {"weekly": 7, "biweekly": 14}


def _with_flags(r: dict[str, Any]) -> dict[str, Any]:
    r["declared"] = r["promoted_to_rule_id"] is not None
    r["suggested"] = not r["declared"] and not r["dismissed"] and r["status"] == "active"
    return r


def list_detected(
    s: Session, user_id: uuid.UUID, include_all: bool = False
) -> list[dict[str, Any]]:
    """Open suggestions by default; `include_all` adds declared, dismissed and lapsed series."""
    where = (
        ""
        if include_all
        else " AND d.status = 'active' AND NOT d.dismissed AND d.promoted_to_rule_id IS NULL"
    )
    rows = s.execute(
        text(
            _SELECT
            + " AND d.status <> 'gone'"
            + where
            + " ORDER BY d.confidence DESC, d.amount_typical DESC"
        ),
        {"u": user_id},
    ).mappings()
    return [_with_flags(dict(r)) for r in rows]


def get_detected(s: Session, user_id: uuid.UUID, det_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(text(_SELECT + " AND d.id = :id"), {"u": user_id, "id": det_id})
        .mappings()
        .first()
    )
    if row is None or row["status"] == "gone":
        raise RecurringError("Detected recurring item not found.", 404)
    return _with_flags(dict(row))


def next_start(period: str, nxt: dt.date, today: dt.date) -> dt.date:
    """First expected date strictly after today (the charge due today may still post from the bank)."""
    day = nxt.day
    k = 0
    d = nxt
    while d <= today:
        k += 1
        d = (
            _add_months(nxt, MONTH_STEPS[period] * k, day)
            if period in MONTH_STEPS
            else nxt + dt.timedelta(days=DAY_STEPS[period] * k)
        )
    return d


def promote(s: Session, user_id: uuid.UUID, det_id: uuid.UUID) -> dict[str, Any]:
    d = get_detected(s, user_id, det_id)
    if d["promoted_to_rule_id"] is not None:
        raise RecurringError("This item is already a recurring rule.", 409)
    start = next_start(d["period"], d["next_expected"], rec.user_today(s, user_id))
    data: dict[str, Any] = {
        "kind": d["kind"],
        "description": d["merchant_norm"].title()[:200],
        "amount": d["amount_typical"],
        "currency": d["currency"],
        "category_id": d["category_id"],
        "frequency": d["period"],
        "start_date": start,
        "notes": "Created from a detected repeat",
    }
    if d["period"] in MONTH_STEPS:
        data["day_of_month"] = start.day
    else:
        data["weekday"] = start.weekday()
    rule = rec.create_rule(s, user_id, data)
    s.execute(
        text(
            "UPDATE detected_recurrences SET promoted_to_rule_id = :r, dismissed = false WHERE id = :id AND user_id = :u"
        ),
        {"r": rule["id"], "id": det_id, "u": user_id},
    )
    invalidate(s, user_id)
    return {"detected": get_detected(s, user_id, det_id), "rule": rule}


def dismiss(
    s: Session, user_id: uuid.UUID, det_id: uuid.UUID, dismissed: bool = True
) -> dict[str, Any]:
    get_detected(s, user_id, det_id)
    s.execute(
        text("""UPDATE detected_recurrences SET dismissed = :d, dismissed_at = CASE WHEN :d THEN now() END
                WHERE id = :id AND user_id = :u"""),
        {"d": dismissed, "id": det_id, "u": user_id},
    )
    invalidate(s, user_id)
    return get_detected(s, user_id, det_id)


def set_usage(
    s: Session, user_id: uuid.UUID, det_id: uuid.UUID, rating: int | None
) -> dict[str, Any]:
    d = get_detected(s, user_id, det_id)
    if d["kind"] != "expense":
        raise RecurringError("Only recurring expenses can be rated.", 422)
    if rating is not None and not 0 <= rating <= 5:
        raise RecurringError("Rating must be between 0 and 5.", 422)
    n = rowcount(
        s.execute(
            text("""UPDATE detected_recurrences SET usage_rating = CAST(:r AS smallint),
                    usage_rated_at = CASE WHEN CAST(:r AS smallint) IS NULL THEN NULL ELSE now() END
                    WHERE id = :id AND user_id = :u"""),
            {"r": rating, "id": det_id, "u": user_id},
        )
    )
    if n != 1:
        raise RecurringError("Detected recurring item not found.", 404)
    invalidate(s, user_id)
    return get_detected(s, user_id, det_id)


def upcoming_detected(
    s: Session, user_id: uuid.UUID, today: dt.date, days: int = 14
) -> list[dict[str, Any]]:
    """Expected charges of active, undeclared, undismissed series in the next `days` (dashboard)."""
    rows = s.execute(
        text(
            _SELECT
            + """ AND d.status = 'active' AND NOT d.dismissed AND d.promoted_to_rule_id IS NULL
                          AND d.next_expected BETWEEN :a AND :b ORDER BY d.next_expected"""
        ),
        {"u": user_id, "a": today, "b": today + dt.timedelta(days=days)},
    ).mappings()
    return [
        {
            "date": r["next_expected"],
            "description": r["merchant_norm"].title(),
            "kind": r["kind"],
            "amount": r["amount_typical"],
            "currency": r["currency"],
            "detected_id": r["id"],
            "estimated": True,
        }
        for r in rows
    ]
