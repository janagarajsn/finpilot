"""What-if simulator: re-runs the forecaster with the SAME seed and the scenario applied.

Verdict (explicit rule, thresholds from settings / currencies.yml):
  affordable      the p10 (bad-case) balance stays at or above the cash buffer for the whole horizon
  risky           the p50 balance stays above the buffer, but p10 dips below it
  not_affordable  otherwise
Levers: candidate adjustments (delay, installments, cut the top discretionary category, cancel the
priciest subscription) are each RE-SIMULATED; only levers that verifiably raise the worst-case
balance are returned, best two first.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finpilot.categories.rules import thresholds
from finpilot.forecasting.inputs import Context
from finpilot.forecasting.model import Bands, ForecastInputs, simulate
from finpilot.forecasting.scenarios import (
    Applier,
    CancelSubscription,
    CategoryCut,
    Composite,
    LoanOrInstallments,
    OneTimePurchase,
)

VERDICT_RANK = {"not_affordable": 0, "risky": 1, "affordable": 2}


def money(x: float) -> Decimal:
    return Decimal(str(round(float(x), 2))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class Outcome(BaseModel):
    verdict: str
    min_p10: Decimal
    min_p10_date: dt.date
    min_p50: Decimal
    min_p50_date: dt.date
    end_p10: Decimal
    end_p50: Decimal
    end_p90: Decimal
    end_mean: Decimal
    p_below_zero: float
    p_below_buffer: float
    first_below_buffer_p10: dt.date | None


def verdict(b: Bands, buffer: float) -> str:
    if float(b.p10.min()) >= buffer:
        return "affordable"
    if float(b.p50.min()) >= buffer:
        return "risky"
    return "not_affordable"


def outcome(b: Bands, buffer: float) -> Outcome:
    i10, i50 = int(np.argmin(b.p10)), int(np.argmin(b.p50))
    below = np.flatnonzero(b.p10 < buffer)
    return Outcome(
        verdict=verdict(b, buffer),
        min_p10=money(b.p10[i10]),
        min_p10_date=b.dates[i10],
        min_p50=money(b.p50[i50]),
        min_p50_date=b.dates[i50],
        end_p10=money(b.p10[-1]),
        end_p50=money(b.p50[-1]),
        end_p90=money(b.p90[-1]),
        end_mean=money(b.mean[-1]),
        p_below_zero=round(b.p_below(0.0), 4),
        p_below_buffer=round(b.p_below(buffer), 4),
        first_below_buffer_p10=b.dates[int(below[0])] if len(below) else None,
    )


class Lever(BaseModel):
    kind: str  # delay | installments | category_cut | cancel_subscription
    title_data: dict[str, Any]
    scenario: dict[str, Any]
    outcome: Outcome
    improvement: Decimal  # rise of the worst-case (p10) minimum balance vs the scenario


class SimulationResult(BaseModel):
    currency: str
    as_of: dt.date
    horizon: int
    buffer: Decimal
    scenario: dict[str, Any]
    applied: list[dict[str, Any]]
    fx_used: list[dict[str, Any]]
    baseline: Outcome
    result: Outcome
    end_delta_mean: Decimal  # scenario − baseline, expected end balance
    end_delta_p50: Decimal
    p_overdraft_change: float
    levers: list[Lever]
    series: dict[str, list[Any]]  # dates + baseline/scenario p10/p50/p90 for the chart
    low_confidence: bool
    seed: int
    n_paths: int
    forecast_run_id: str | None = None


def _top_discretionary(base: ForecastInputs, b: Bands) -> tuple[str, str] | None:
    st = base.streams
    best = None
    for c, (key, label, nature) in enumerate(zip(st.keys, st.labels, st.natures, strict=True)):
        if nature != "discretionary":
            continue
        spend = -float(b.stream_mean[:, c].sum())
        if spend > 0 and (best is None or spend > best[0]):
            best = (spend, key, label)
    return (best[1], best[2]) if best else None


def _candidates(
    sc: Any, base: ForecastInputs, ctx: Context, b: Bands
) -> list[tuple[str, dict, Any]]:
    cfg = thresholds()["simulator"]
    out: list[tuple[str, dict, Any]] = []
    parts = sc.parts if sc.type == "composite" else [sc]
    end = base.as_of + dt.timedelta(days=base.horizon)
    for i, p in enumerate(parts):
        if p.type == "one_time_purchase":
            when = p.date or base.as_of + dt.timedelta(days=1)
            for days in cfg["lever_delay_days"]:
                d = when + dt.timedelta(days=int(days))
                if d <= end:
                    moved = p.model_copy(update={"date": d})
                    out.append(
                        (
                            "delay",
                            {"days": int(days), "date": d.isoformat()},
                            _swap(parts, i, moved),
                        )
                    )
            n = int(cfg["lever_installments"])
            inst = LoanOrInstallments(
                total=p.amount, n=n, apr=0, currency=p.currency, start_date=when, label=p.label
            )
            out.append(("installments", {"n": n}, _swap(parts, i, inst)))
    top = _top_discretionary(base, b)
    if top:
        pct = float(cfg["lever_category_cut_pct"])
        out.append(
            (
                "category_cut",
                {"category": top[1], "pct": pct},
                Composite(parts=[*parts, CategoryCut(category=top[0], pct=pct)]),
            )
        )
    cancelled = {p.subscription_id for p in parts if p.type == "cancel_subscription"}
    subs = [
        d for d in ctx.detected
        if d["kind"] == "expense" and d["status"] == "active" and not d["dismissed"] and str(d["id"]) not in cancelled
        and (d.get("category_slug") in ("subscriptions", "entertainment"))
    ]  # fmt: skip
    if subs:
        top_sub = max(subs, key=lambda d: d["amount_typical"])
        out.append(
            (
                "cancel_subscription",
                {"merchant": top_sub["merchant_norm"]},
                Composite(parts=[*parts, CancelSubscription(subscription_id=str(top_sub["id"]))]),
            )
        )
    return out


def _swap(parts: list[Any], i: int, new: Any) -> Any:
    ps = list(parts)
    ps[i] = new
    return ps[0] if len(ps) == 1 else Composite(parts=ps)


def run(
    s: Session, sc: Any, base: ForecastInputs, ctx: Context, with_levers: bool = True
) -> SimulationResult:
    buffer = base.buffer
    applier = Applier(s, base, ctx)
    scn_inp = applier.apply(sc)
    b0, b1 = simulate(base), simulate(scn_inp)
    o0, o1 = outcome(b0, buffer), outcome(b1, buffer)
    levers: list[Lever] = []
    if with_levers and o1.verdict != "affordable":
        for kind, data, cand in _candidates(sc, base, ctx, b0):
            try:
                lv_inp = Applier(s, base, ctx).apply(cand)
            except Exception:  # noqa: S112 — a lever that does not apply is simply not offered
                continue
            lo = outcome(simulate(lv_inp), buffer)
            gain = lo.min_p10 - o1.min_p10
            if gain > 0:
                levers.append(
                    Lever(
                        kind=kind,
                        title_data=data,
                        scenario=cand.model_dump(mode="json"),
                        outcome=lo,
                        improvement=gain,
                    )
                )
        levers.sort(key=lambda lv: (-VERDICT_RANK[lv.outcome.verdict], -lv.improvement))
        levers = levers[:2]
    return SimulationResult(
        currency=base.currency,
        as_of=base.as_of,
        horizon=base.horizon,
        buffer=money(buffer),
        scenario=sc.model_dump(mode="json"),
        applied=applier.applied,
        fx_used=applier.fx_used,
        baseline=o0,
        result=o1,
        end_delta_mean=money(b1.mean[-1] - b0.mean[-1]),
        end_delta_p50=money(b1.p50[-1] - b0.p50[-1]),
        p_overdraft_change=round(o1.p_below_zero - o0.p_below_zero, 4),
        levers=levers,
        series={
            "dates": [d.isoformat() for d in b0.dates],
            "base_p10": [round(float(x), 2) for x in b0.p10],
            "base_p50": [round(float(x), 2) for x in b0.p50],
            "base_p90": [round(float(x), 2) for x in b0.p90],
            "scn_p10": [round(float(x), 2) for x in b1.p10],
            "scn_p50": [round(float(x), 2) for x in b1.p50],
            "scn_p90": [round(float(x), 2) for x in b1.p90],
        },
        low_confidence=base.low_confidence,
        seed=base.seed,
        n_paths=base.n_paths,
    )


__all__ = ["Lever", "OneTimePurchase", "Outcome", "SimulationResult", "outcome", "run", "verdict"]
