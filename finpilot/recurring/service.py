"""User-declared recurring rules: CRUD + lifecycle. Materialization lives in materializer.py.

Editing a rule changes FUTURE occurrences only: already-materialized transactions are kept.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
import zoneinfo
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.money import get_currency_registry, quantize
from finpilot.recurring.rules import Schedule, next_n, next_occurrence

FREQUENCIES = ("daily", "weekly", "biweekly", "monthly", "quarterly", "yearly")
EDITABLE = {
    "kind",
    "description",
    "amount",
    "currency",
    "category_id",
    "account_id",
    "frequency",
    "interval",
    "day_of_month",
    "weekday",
    "start_date",
    "end_date",
    "notes",
}


class RecurringError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def local_today(tz: str) -> dt.date:
    try:
        return dt.datetime.now(zoneinfo.ZoneInfo(tz)).date()
    except Exception:
        return dt.datetime.now(dt.UTC).date()


def user_today(s: Session, user_id: uuid.UUID) -> dt.date:
    tz = s.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id}).scalar_one()
    return local_today(tz)


def schedule_of(r: dict[str, Any]) -> Schedule:
    skips = r.get("skip_dates_json") or []
    if isinstance(skips, str):
        skips = json.loads(skips)
    return Schedule(
        frequency=r["frequency"],
        start_date=r["start_date"],
        interval=int(r.get("interval") or 1),
        day_of_month=r.get("day_of_month"),
        weekday=r.get("weekday"),
        end_date=r.get("end_date"),
        skip_dates=frozenset(dt.date.fromisoformat(d) for d in skips),
    )


def _validate(s: Session, user_id: uuid.UUID, d: dict[str, Any]) -> dict[str, Any]:
    if d.get("kind") not in ("expense", "income"):
        raise RecurringError("Type must be expense or income.", 422)
    if d.get("frequency") not in FREQUENCIES:
        raise RecurringError("Unknown frequency.", 422)
    desc = " ".join(str(d.get("description") or "").split())
    if not 1 <= len(desc) <= 200:
        raise RecurringError("Description is required (max 200 characters).", 422)
    d["description"] = desc
    amount = Decimal(str(d.get("amount")))
    if amount <= 0:
        raise RecurringError("Amount must be greater than zero.", 422)
    currency = (d.get("currency") or "").upper()
    if not currency:
        currency = s.execute(
            text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
    if not get_currency_registry().is_supported(currency):
        raise RecurringError(f"Unsupported currency {currency}.", 422)
    d["currency"], d["amount"] = currency, quantize(amount, currency)
    if d.get("category_id"):
        try:
            categories.require_visible(s, user_id, d["category_id"])
        except categories.CategoryError as e:
            raise RecurringError(str(e), e.status) from e
    if (
        d.get("account_id")
        and not s.execute(
            text("SELECT 1 FROM accounts WHERE id = :a AND user_id = :u"),
            {"a": d["account_id"], "u": user_id},
        ).first()
    ):
        raise RecurringError("Account not found.", 404)
    try:
        schedule_of({**d, "skip_dates_json": d.get("skip_dates_json") or []})
    except ValueError as e:
        raise RecurringError(str(e), 422) from e
    return d


def get_rule(s: Session, user_id: uuid.UUID, rule_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("SELECT * FROM recurring_rules WHERE id = :id AND user_id = :u"),
            {"id": rule_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RecurringError("Recurring item not found.", 404)
    return dict(row)


def with_preview(
    s: Session, user_id: uuid.UUID, r: dict[str, Any], today: dt.date | None = None
) -> dict[str, Any]:
    today = today or user_today(s, user_id)
    nxt = (
        next_n(schedule_of(r), max(today, r["next_run_date"] or today), 5)
        if r["status"] == "active"
        else []
    )
    cat = categories.get_category(s, user_id, r["category_id"]) if r["category_id"] else None
    return {
        **r,
        "next_occurrences": nxt,
        "category_name": cat["name"] if cat else None,
        "category_color": cat["color"] if cat else None,
    }


def list_rules(s: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    today = user_today(s, user_id)
    rows = s.execute(
        text("""SELECT * FROM recurring_rules WHERE user_id = :u
                             ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                                      next_run_date NULLS LAST, lower(description)"""),
        {"u": user_id},
    ).mappings()
    return [with_preview(s, user_id, dict(r), today) for r in rows]


def _recompute_next(r: dict[str, Any], from_date: dt.date) -> tuple[dt.date | None, str]:
    nxt = next_occurrence(schedule_of(r), from_date)
    if nxt is None:
        return None, "ended"
    return nxt, r["status"] if r["status"] != "ended" else "active"


def create_rule(
    s: Session, user_id: uuid.UUID, data: dict[str, Any], first_txn_id: uuid.UUID | None = None
) -> dict[str, Any]:
    d = _validate(s, user_id, {k: v for k, v in data.items() if k in EDITABLE})
    nxt = next_occurrence(schedule_of({**d, "skip_dates_json": []}), d["start_date"])
    rid = s.execute(
        text("""
        INSERT INTO recurring_rules (user_id, kind, description, amount, currency, category_id, account_id,
          frequency, interval, day_of_month, weekday, start_date, end_date, next_run_date, status, notes)
        VALUES (:u, :kind, :description, :amount, :currency, :category_id, :account_id, :frequency,
          :interval, :day_of_month, :weekday, :start_date, :end_date, :nxt, :status, :notes)
        RETURNING id"""),
        {
            "u": user_id,
            "kind": d["kind"],
            "description": d["description"],
            "amount": d["amount"],
            "currency": d["currency"],
            "category_id": d.get("category_id"),
            "account_id": d.get("account_id"),
            "frequency": d["frequency"],
            "interval": d.get("interval") or 1,
            "day_of_month": d.get("day_of_month"),
            "weekday": d.get("weekday"),
            "start_date": d["start_date"],
            "end_date": d.get("end_date"),
            "nxt": nxt,
            "status": "active" if nxt else "ended",
            "notes": d.get("notes"),
        },
    ).scalar_one()
    if first_txn_id is not None and nxt is not None:
        # "Repeat this" from a manual transaction: that transaction IS the first occurrence.
        linked = s.execute(
            text("""UPDATE transactions SET recurring_rule_id = :r WHERE id = :t AND user_id = :u
                                   AND date = :d RETURNING date"""),
            {"r": rid, "t": first_txn_id, "u": user_id, "d": nxt},
        ).first()
        if linked:
            r = get_rule(s, user_id, rid)
            n2, status = _recompute_next(r, nxt + dt.timedelta(days=1))
            s.execute(
                text("""UPDATE recurring_rules SET last_materialized_date = :d, next_run_date = :n,
                              status = :st WHERE id = :r AND user_id = :u"""),
                {"d": nxt, "n": n2, "st": status, "r": rid, "u": user_id},
            )
    return with_preview(s, user_id, get_rule(s, user_id, rid))


def update_rule(
    s: Session, user_id: uuid.UUID, rule_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    cur = get_rule(s, user_id, rule_id)
    merged = {**cur, **{k: v for k, v in changes.items() if k in EDITABLE}}
    d = _validate(s, user_id, merged)
    today = user_today(s, user_id)
    floor = max(
        today,
        (cur["last_materialized_date"] or dt.date.min) + dt.timedelta(days=1),
        d["start_date"],
    )
    nxt, status = (
        (None, cur["status"])
        if cur["status"] == "ended"
        else _recompute_next({**d, "status": cur["status"]}, floor)
    )
    sets = {k: d.get(k) for k in EDITABLE}
    assign = ", ".join(f"{k} = :{k}" for k in sets)
    s.execute(
        text(
            f"UPDATE recurring_rules SET {assign}, next_run_date = :nxt, status = :st "  # noqa: S608 — allowlisted
            "WHERE id = :id AND user_id = :u"
        ),
        {**sets, "nxt": nxt, "st": status, "id": rule_id, "u": user_id},
    )
    return with_preview(s, user_id, get_rule(s, user_id, rule_id))


def set_status(s: Session, user_id: uuid.UUID, rule_id: uuid.UUID, action: str) -> dict[str, Any]:
    r = get_rule(s, user_id, rule_id)
    today = user_today(s, user_id)
    if action == "pause":
        if r["status"] != "active":
            raise RecurringError("Only active items can be paused.", 409)
        s.execute(
            text("UPDATE recurring_rules SET status = 'paused' WHERE id = :id AND user_id = :u"),
            {"id": rule_id, "u": user_id},
        )
    elif action == "resume":
        if r["status"] != "paused":
            raise RecurringError("Only paused items can be resumed.", 409)
        # Occurrences that fell inside the pause are NOT created.
        floor = max(today, (r["last_materialized_date"] or dt.date.min) + dt.timedelta(days=1))
        nxt, status = _recompute_next({**r, "status": "active"}, floor)
        s.execute(
            text(
                "UPDATE recurring_rules SET status = :st, next_run_date = :n WHERE id = :id AND user_id = :u"
            ),
            {"st": status, "n": nxt, "id": rule_id, "u": user_id},
        )
    elif action == "skip-next":
        if r["status"] != "active" or r["next_run_date"] is None:
            raise RecurringError("Nothing to skip.", 409)
        skips = sorted({*(r["skip_dates_json"] or []), r["next_run_date"].isoformat()})
        r2 = {**r, "skip_dates_json": skips}
        nxt, status = _recompute_next(r2, r["next_run_date"])
        s.execute(
            text("""UPDATE recurring_rules SET skip_dates_json = CAST(:sk AS jsonb), next_run_date = :n,
                          status = :st WHERE id = :id AND user_id = :u"""),
            {"sk": json.dumps(skips), "n": nxt, "st": status, "id": rule_id, "u": user_id},
        )
    elif action == "end":
        s.execute(
            text("""UPDATE recurring_rules SET status = 'ended', next_run_date = NULL,
                          end_date = coalesce(end_date, :t) WHERE id = :id AND user_id = :u"""),
            {"t": max(today, r["start_date"]), "id": rule_id, "u": user_id},
        )
    else:
        raise RecurringError("Unknown action.", 422)
    return with_preview(s, user_id, get_rule(s, user_id, rule_id), today)


def delete_rule(s: Session, user_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    get_rule(s, user_id, rule_id)
    s.execute(
        text("DELETE FROM recurring_rules WHERE id = :id AND user_id = :u"),
        {"id": rule_id, "u": user_id},
    )


def upcoming(s: Session, user_id: uuid.UUID, days: int = 14) -> list[dict[str, Any]]:
    """Declared bills/incomes due in the next `days` (dashboard)."""
    today = user_today(s, user_id)
    end = today + dt.timedelta(days=days)
    out: list[dict[str, Any]] = []
    for r in s.execute(
        text("SELECT * FROM recurring_rules WHERE user_id = :u AND status = 'active'"),
        {"u": user_id},
    ).mappings():
        sched = schedule_of(dict(r))
        for d in next_n(sched, max(today, r["next_run_date"] or today), 10):
            if d > end:
                break
            out.append(
                {
                    "date": d,
                    "description": r["description"],
                    "kind": r["kind"],
                    "amount": r["amount"],
                    "currency": r["currency"],
                    "rule_id": r["id"],
                }
            )
    return sorted(out, key=lambda x: x["date"])
