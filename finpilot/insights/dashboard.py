"""Dashboard v1: deterministic aggregates from SQL (reporting currency). No LLM.

Phase 8 persists these into `insight_snapshots` and pre-warms caches; until then the
dashboard computes live (fast: a handful of indexed aggregate queries).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.db.sql_loader import sql
from finpilot.fx.service import FXService
from finpilot.insights.models import AnalysisReport, Finding
from finpilot.money import quantize
from finpilot.recurring import detected
from finpilot.recurring import service as rec
from finpilot.transactions import service as txns

Period = Literal["this_month", "30d", "3m", "6m", "12m"]
PERIODS: dict[str, str] = {
    "this_month": "This month",
    "30d": "Last 30 days",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
    "12m": "Last 12 months",
}


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date
    prev_start: dt.date
    prev_end: dt.date
    label: str


def window(period: str, today: dt.date) -> Window:
    if period == "this_month":
        start = today.replace(day=1)
        prev_end = start - dt.timedelta(days=1)
        # same number of elapsed days in the previous month (fair month-to-date comparison)
        prev_start = prev_end.replace(day=1)
        prev_end = min(prev_end, prev_start + (today - start))
        return Window(start, today, prev_start, prev_end, PERIODS[period])
    if period == "last_month":  # full previous calendar month vs the one before (answers only)
        end = today.replace(day=1) - dt.timedelta(days=1)
        start = end.replace(day=1)
        prev_end = start - dt.timedelta(days=1)
        return Window(start, end, prev_end.replace(day=1), prev_end, "Last month")
    days = {"30d": 30, "3m": 91, "6m": 182, "12m": 365}.get(period, 30)
    start = today - dt.timedelta(days=days - 1)
    return Window(
        start,
        today,
        start - dt.timedelta(days=days),
        start - dt.timedelta(days=1),
        PERIODS.get(period, PERIODS["30d"]),
    )


def _pct(cur: Decimal, prev: Decimal) -> float | None:
    if prev == 0:
        return None
    return float(round((cur - prev) / prev * 100, 1))


def _totals(s: Session, uid: uuid.UUID, start: dt.date, end: dt.date) -> dict[str, Any]:
    r = s.execute(sql("period_totals"), {"u": uid, "start": start, "end": end}).mappings().one()
    income, spend = Decimal(r["income"]), Decimal(r["spend"])
    return {
        "income": income,
        "spend": spend,
        "saved": Decimal(r["saved"]),
        "net": income - spend,
        "savings_rate": float(round((income - spend) / income * 100, 1)) if income > 0 else None,
        "txn_count": r["txn_count"],
        "missing_rate_count": r["missing_rate_count"],
    }


def balance(
    s: Session,
    uid: uuid.UUID,
    reporting: str,
    fx: FXService | None,
    as_of: dt.date | None = None,
) -> tuple[Decimal, bool]:
    """Opening balances (converted at the latest rate) + transactions (up to `as_of` if given).
    Returns (balance, approximate)."""
    net_row = s.execute(sql("lifetime_net"), {"u": uid, "as_of": as_of}).mappings().one()
    total, approx = Decimal(net_row["net"]), net_row["missing"] > 0
    for a in s.execute(
        text("SELECT currency, opening_balance FROM accounts WHERE user_id = :u AND NOT archived"),
        {"u": uid},
    ):
        ob = Decimal(a.opening_balance)
        if ob == 0:
            continue
        if a.currency == reporting:
            total += ob
        elif fx is not None:
            conv = fx.convert(ob, a.currency, reporting)
            total += conv.converted
            approx = approx or conv.approximate or conv.stale
        else:
            approx = True
    return quantize(total, reporting), approx


def key_insights(report: AnalysisReport, n: int = 3) -> list[Finding]:
    """Dashboard cards: the top anomaly, the top subscription flag, then the next most severe."""
    picks: list[Finding] = []
    for kind in ("anomaly", "subscription"):
        picks += [f for f in report.top(kind, 1) if f.severity != "info"]
    rest = [
        f
        for f in sorted(report.findings, key=lambda f: f.sort_key)
        if f not in picks and f.severity != "info"
    ]
    return (picks + rest)[:n]


def build(
    s: Session, uid: uuid.UUID, period: str = "this_month", fx: FXService | None = None
) -> dict[str, Any]:
    user = (
        s.execute(
            text("SELECT reporting_currency, timezone, data_version FROM users WHERE id = :u"),
            {"u": uid},
        )
        .mappings()
        .one()
    )
    reporting = user["reporting_currency"]
    today = rec.local_today(user["timezone"])
    w = window(period if period in PERIODS or period == "last_month" else "this_month", today)
    cur, prev = _totals(s, uid, w.start, w.end), _totals(s, uid, w.prev_start, w.prev_end)
    bal, bal_approx = balance(s, uid, reporting, fx)
    trend = [
        dict(r)
        for r in s.execute(sql("monthly_trend"), {"u": uid, "end": today, "months": 12}).mappings()
    ]
    breakdown = [
        dict(r)
        for r in s.execute(
            sql("category_breakdown"), {"u": uid, "start": w.start, "end": w.end}
        ).mappings()
    ]
    recent, total_count = txns.list_(s, uid, txns.TxnFilter(limit=8))
    # declared rules first; detected-but-undeclared repeats are shown as estimates
    upcoming = sorted(
        rec.upcoming(s, uid, days=14) + detected.upcoming_detected(s, uid, today, days=14),
        key=lambda u: u["date"],
    )
    review = s.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u AND needs_review"), {"u": uid}
    ).scalar_one()
    uncategorized = s.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u AND category_id IS NULL"),
        {"u": uid},
    ).scalar_one()
    return {
        "as_of": today,
        "period": period,
        "period_label": w.label,
        "window": [w.start, w.end],
        "reporting_currency": reporting,
        "data_version": user["data_version"],
        "kpis": {
            "balance": bal,
            "balance_approximate": bal_approx,
            "income": cur["income"],
            "spend": cur["spend"],
            "net": cur["net"],
            "saved": cur["saved"],
            "savings_rate": cur["savings_rate"],
            "income_delta_pct": _pct(cur["income"], prev["income"]),
            "spend_delta_pct": _pct(cur["spend"], prev["spend"]),
            "spend_prev": prev["spend"],
            "income_prev": prev["income"],
            "savings_rate_prev": prev["savings_rate"],
            "missing_rate_count": cur["missing_rate_count"],
        },
        "trend": trend,
        "breakdown": breakdown,
        "recent": recent,
        "upcoming": upcoming,
        "counts": {
            "transactions": total_count,
            "needs_review": review,
            "uncategorized": uncategorized,
        },
    }
