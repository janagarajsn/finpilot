"""Goals: CRUD (financial facts → data_version triggers) and deterministic projection.

savings_target: remaining = target − saved; ETA from the forecast's projected monthly savings;
                on track if the ETA is on or before target_date (when set).
spend_cap:      projected spend this month in the category = month-to-date actual + expected rest
                of month from the category's variable stream; compared with the cap.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.money import get_currency_registry, quantize

EDITABLE = {
    "kind",
    "name",
    "target_amount",
    "currency",
    "saved_amount",
    "target_date",
    "category_id",
    "status",
}


class GoalError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def list_goals(
    s: Session, user_id: uuid.UUID, include_archived: bool = False
) -> list[dict[str, Any]]:
    rows = s.execute(
        text("""SELECT g.*, c.name AS category_name FROM goals g LEFT JOIN categories c ON c.id = g.category_id
                WHERE g.user_id = :u AND (:all OR g.status <> 'archived') ORDER BY g.created_at"""),
        {"u": user_id, "all": include_archived},
    ).mappings()
    return [dict(r) for r in rows]


def get_goal(s: Session, user_id: uuid.UUID, goal_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("""SELECT g.*, c.name AS category_name FROM goals g LEFT JOIN categories c ON c.id = g.category_id
                    WHERE g.id = :id AND g.user_id = :u"""),
            {"id": goal_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise GoalError("Goal not found.", 404)
    return dict(row)


def _validate(s: Session, user_id: uuid.UUID, d: dict[str, Any]) -> dict[str, Any]:
    if d.get("kind") not in ("savings_target", "spend_cap", "debt_payoff"):
        raise GoalError("Goal type must be savings_target, spend_cap or debt_payoff.")
    name = " ".join(str(d.get("name") or "").split())
    if not 1 <= len(name) <= 100:
        raise GoalError("Name is required (max 100 characters).")
    d["name"] = name
    cur = (d.get("currency") or "").upper() or s.execute(
        text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    if not get_currency_registry().is_supported(cur):
        raise GoalError(f"Unsupported currency {cur}.")
    d["currency"] = cur
    amt = Decimal(str(d.get("target_amount") or 0))
    if amt <= 0:
        raise GoalError("Target amount must be greater than zero.")
    d["target_amount"] = quantize(amt, cur)
    d["saved_amount"] = quantize(Decimal(str(d.get("saved_amount") or 0)), cur)
    if d["saved_amount"] < 0:
        raise GoalError("Saved amount cannot be negative.")
    if d["kind"] in ("spend_cap", "debt_payoff") and d.get("category_id"):
        try:
            categories.require_visible(s, user_id, d["category_id"])
        except categories.CategoryError as e:
            raise GoalError(str(e), e.status) from e
    elif d["kind"] == "spend_cap":
        raise GoalError("A spending cap needs a category.")
    else:
        d["category_id"] = None
    if d.get("status", "active") not in ("active", "achieved", "archived"):
        raise GoalError("Unknown status.")
    return d


def create_goal(s: Session, user_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    d = _validate(s, user_id, {k: v for k, v in data.items() if k in EDITABLE})
    gid = s.execute(
        text("""INSERT INTO goals (user_id, kind, name, target_amount, currency, saved_amount, target_date, category_id)
                VALUES (:u, :kind, :name, :target_amount, :currency, :saved_amount, :target_date, :category_id)
                RETURNING id"""),
        {
            "u": user_id,
            "target_date": d.get("target_date"),
            **{
                k: d[k]
                for k in (
                    "kind",
                    "name",
                    "target_amount",
                    "currency",
                    "saved_amount",
                    "category_id",
                )
            },
        },
    ).scalar_one()
    return get_goal(s, user_id, gid)


def update_goal(
    s: Session, user_id: uuid.UUID, goal_id: uuid.UUID, patch: dict[str, Any]
) -> dict[str, Any]:
    cur = get_goal(s, user_id, goal_id)
    d = _validate(
        s,
        user_id,
        {**{k: cur[k] for k in EDITABLE}, **{k: v for k, v in patch.items() if k in EDITABLE}},
    )
    s.execute(
        text("""UPDATE goals SET kind = :kind, name = :name, target_amount = :target_amount, currency = :currency,
                saved_amount = :saved_amount, target_date = :target_date, category_id = :category_id, status = :status
                WHERE id = :id AND user_id = :u"""),
        {
            "id": goal_id,
            "u": user_id,
            "status": d.get("status", "active"),
            "target_date": d.get("target_date"),
            **{
                k: d[k]
                for k in (
                    "kind",
                    "name",
                    "target_amount",
                    "currency",
                    "saved_amount",
                    "category_id",
                )
            },
        },
    )
    return get_goal(s, user_id, goal_id)


def delete_goal(s: Session, user_id: uuid.UUID, goal_id: uuid.UUID) -> None:
    get_goal(s, user_id, goal_id)
    s.execute(
        text("DELETE FROM goals WHERE id = :id AND user_id = :u"), {"id": goal_id, "u": user_id}
    )


# ---------------------------------------------------------------------------- projection
def _q(x: float | Decimal, cur: str) -> Decimal:
    return quantize(Decimal(str(round(float(x), 2))), cur)


def project(
    goal: dict[str, Any],
    *,
    as_of: dt.date,
    currency: str,
    monthly_savings: float,
    rate: Decimal,
    stream_expected: dict[str, list[tuple[dt.date, float]]],
    frame_df: pd.DataFrame,
    scheduled_monthly: dict[str, float] | None = None,
) -> dict[str, Any]:
    """`rate` converts the goal currency into the reporting currency (1 when equal).
    `scheduled_monthly`: expected monthly outflow per category from scheduled items (debt payoff)."""
    target = Decimal(goal["target_amount"]) * rate
    out: dict[str, Any] = {
        "id": str(goal["id"]),
        "kind": goal["kind"],
        "name": goal["name"],
        "currency": currency,
        "target_date": goal["target_date"].isoformat() if goal.get("target_date") else None,
    }
    if goal["kind"] == "savings_target":
        saved = Decimal(goal["saved_amount"]) * rate
        remaining = max(target - saved, Decimal(0))
        out.update(
            target=_q(target, currency),
            saved=_q(saved, currency),
            remaining=_q(remaining, currency),
            progress_pct=round(float(saved / target * 100), 1) if target else 0.0,
            monthly_savings=_q(monthly_savings, currency),
        )
        if remaining == 0:
            out.update(status="reached", eta=as_of.isoformat())
        elif monthly_savings <= 0:
            out.update(status="not_on_track", eta=None, shortfall_reason="no projected savings")
        else:
            months = float(remaining) / monthly_savings
            eta = as_of + dt.timedelta(days=round(months * 30.44))
            out.update(eta=eta.isoformat(), months_to_go=round(months, 1))
            td = goal.get("target_date")
            if td:
                left = max((td - as_of).days / 30.44, 0.01)
                out["required_monthly"] = _q(float(remaining) / left, currency)
                out["status"] = "on_track" if eta <= td else "behind"
            else:
                out["status"] = "on_track"
        return out
    if goal["kind"] == "debt_payoff":
        # target = the debt, saved_amount = repaid so far; pace = expected monthly payments in the
        # loan's category (declared/detected EMIs + usual variable payments)
        paid = Decimal(goal["saved_amount"]) * rate
        remaining = max(target - paid, Decimal(0))
        key = str(goal["category_id"]) if goal.get("category_id") else ""
        pay = (scheduled_monthly or {}).get(key, 0.0) + max(
            -sum(v for _, v in stream_expected.get(key, []))
            / max(len(stream_expected.get(key, [])), 1)
            * 30.44,
            0.0,
        )
        out.update(
            target=_q(target, currency),
            paid=_q(paid, currency),
            remaining=_q(remaining, currency),
            progress_pct=round(float(paid / target * 100), 1) if target else 0.0,
            monthly_payment=_q(pay, currency),
            category=goal.get("category_name"),
        )
        if remaining == 0:
            out.update(status="reached", eta=as_of.isoformat())
        elif pay <= 0:
            out.update(
                status="not_on_track", eta=None, shortfall_reason="no payments in the forecast"
            )
        else:
            months = float(remaining) / pay
            eta = as_of + dt.timedelta(days=round(months * 30.44))
            out.update(eta=eta.isoformat(), months_to_go=round(months, 1))
            td = goal.get("target_date")
            if td:
                left = max((td - as_of).days / 30.44, 0.01)
                out["required_monthly"] = _q(float(remaining) / left, currency)
                out["status"] = "on_track" if eta <= td else "behind"
            else:
                out["status"] = "on_track"
        return out
    # spend cap: this calendar month
    key = str(goal["category_id"])
    month_start = as_of.replace(day=1)
    df = frame_df
    mtd = 0.0
    if not df.empty:
        sel = df[
            (df["category_id"] == key) & (df["date"] >= pd.Timestamp(month_start)) & (df["amt"] < 0)
        ]
        mtd = float(-sel["amt"].sum())
    rest = -sum(
        v
        for d, v in stream_expected.get(key, [])
        if d.month == as_of.month and d.year == as_of.year
    )
    projected = mtd + max(rest, 0.0)
    out.update(
        cap=_q(target, currency),
        month_to_date=_q(mtd, currency),
        projected=_q(projected, currency),
        category=goal.get("category_name"),
    )
    over = Decimal(str(projected)) - target
    out["status"] = "over" if over > 0 else "within"
    out["over_by" if over > 0 else "headroom"] = _q(abs(over), currency).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    return out
