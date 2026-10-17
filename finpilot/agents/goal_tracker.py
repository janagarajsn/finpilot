"""Goal tracker: weekly snapshots (progress, projected completion, on_track, gap) and the
`goal_ahead` finding (ahead of schedule → the advisor suggests reallocating the surplus).

Snapshots are keyed by (goal, ISO week): every pipeline run in a week updates that week's row,
so the history has one point per week and the delta is always against the previous week.
Behind-schedule goals already produce `goal_gap` findings (finpilot/advice/candidates.py).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.forecasting.service import ForecastResult
from finpilot.insights.models import Finding
from finpilot.insights.text import render

AHEAD_DAYS = 30  # projected completion at least a month before the target date


def week_key(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _row(p: dict[str, Any]) -> dict[str, Any]:
    """Map a forecast goal projection to snapshot columns."""
    kind = p["kind"]
    if kind == "spend_cap":
        progress, target = p["projected"], p["cap"]
        gap = p.get("over_by") or Decimal(0)
        on_track = p["status"] == "within"
        eta = None
    else:
        progress = p.get("saved") if kind == "savings_target" else p.get("paid")
        target = p["target"]
        eta = p.get("eta")
        on_track = p["status"] in ("on_track", "reached")
        req, have = p.get("required_monthly"), p.get("monthly_savings") or p.get("monthly_payment")
        gap = (
            max(Decimal(str(req)) - Decimal(str(have or 0)), Decimal(0))
            if req is not None
            else Decimal(0)
        )
    pct = (
        round(float(Decimal(str(progress)) / Decimal(str(target)) * 100), 2)
        if Decimal(str(target))
        else None
    )
    return {
        "progress": progress,
        "target": target,
        "pct": pct,
        "eta": eta,
        "on_track": on_track,
        "gap": gap,
        "status": p["status"],
    }


def snapshot(s: Session, user_id: uuid.UUID, fc: ForecastResult) -> list[dict[str, Any]]:
    """Upsert this week's snapshot per active goal; return each with the delta vs the previous week."""
    wk = week_key(fc.as_of)
    out = []
    for p in fc.goals:
        r = _row(p)
        s.execute(
            text("""INSERT INTO goal_snapshots (user_id, goal_id, week_key, snapshot_date, progress_amount, target_amount,
                    progress_pct, projected_completion, on_track, gap, status, currency, data_json)
                    VALUES (:u, :g, :wk, :d, :prog, :tgt, :pct, :eta, :ok, :gap, :st, :cur, CAST(:data AS jsonb))
                    ON CONFLICT (goal_id, week_key) DO UPDATE SET snapshot_date = EXCLUDED.snapshot_date,
                      progress_amount = EXCLUDED.progress_amount, target_amount = EXCLUDED.target_amount,
                      progress_pct = EXCLUDED.progress_pct, projected_completion = EXCLUDED.projected_completion,
                      on_track = EXCLUDED.on_track, gap = EXCLUDED.gap, status = EXCLUDED.status,
                      data_json = EXCLUDED.data_json, updated_at = now()"""),
            {
                "u": user_id,
                "g": p["id"],
                "wk": wk,
                "d": fc.as_of,
                "prog": r["progress"],
                "tgt": r["target"],
                "pct": r["pct"],
                "eta": r["eta"],
                "ok": r["on_track"],
                "gap": r["gap"],
                "st": r["status"],
                "cur": fc.currency,
                "data": json.dumps(p, default=str),
            },
        )
        prev = (
            s.execute(
                text("""SELECT week_key, progress_amount, projected_completion, status FROM goal_snapshots
                    WHERE goal_id = :g AND user_id = :u AND week_key < :wk ORDER BY week_key DESC LIMIT 1"""),
                {"g": p["id"], "u": user_id, "wk": wk},
            )
            .mappings()
            .first()
        )
        delta = None
        if prev:
            delta = {
                "since_week": prev["week_key"],
                "progress_change": Decimal(str(r["progress"])) - Decimal(prev["progress_amount"]),
                "eta_change_days": (
                    (dt.date.fromisoformat(r["eta"]) - prev["projected_completion"]).days
                    if r["eta"] and prev["projected_completion"]
                    else None
                ),
                "status_before": prev["status"],
            }
        out.append({"goal_id": p["id"], "name": p["name"], "week": wk, **r, "delta": delta})
    return out


def history(
    s: Session, user_id: uuid.UUID, goal_id: uuid.UUID, limit: int = 26
) -> list[dict[str, Any]]:
    rows = s.execute(
        text("""SELECT week_key, snapshot_date, progress_amount, target_amount, progress_pct, projected_completion,
                       on_track, gap, status FROM goal_snapshots WHERE user_id = :u AND goal_id = :g
                ORDER BY week_key DESC LIMIT :n"""),
        {"u": user_id, "g": goal_id, "n": limit},
    ).mappings()
    return [dict(r) for r in rows][::-1]


def ahead_findings(fc: ForecastResult) -> list[Finding]:
    """Savings goals projected to finish ≥ 30 days early with a monthly surplus → goal_ahead."""
    out = []
    for p in fc.goals:
        if (
            p["kind"] != "savings_target"
            or p["status"] != "on_track"
            or not p.get("eta")
            or not p.get("required_monthly")
        ):
            continue
        g_td = p.get("target_date")
        surplus = Decimal(str(p["monthly_savings"])) - Decimal(str(p["required_monthly"]))
        eta = dt.date.fromisoformat(p["eta"])
        td = dt.date.fromisoformat(g_td) if g_td else None
        if surplus <= 0 or (td is not None and (td - eta).days < AHEAD_DAYS):
            continue
        f = Finding(
            fingerprint=f"goal_ahead:{p['id']}:{fc.as_of:%Y-%m}",
            kind="goal",
            type="goal_ahead",
            severity="info",
            metric="monthly_surplus_vs_required",
            baseline=Decimal(str(p["required_monthly"])),
            observed=Decimal(str(p["monthly_savings"])),
            unit="currency",
            currency=fc.currency,
            data={**p, "surplus": surplus.quantize(Decimal("0.01")), "currency": fc.currency},
        )
        render(f)
        out.append(f)
    return out
