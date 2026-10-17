"""Forecast service: build inputs → simulate → typed ForecastResult, persisted in `forecast_runs`
(so recommendations and the decision trail can cite it by id). `get_forecast` reuses the latest
baseline while (data_version, reporting_currency, as_of, horizon) match, else recomputes under a
per-user advisory lock."""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.forecasting import goals as goals_mod
from finpilot.forecasting import inputs as inputs_mod
from finpilot.forecasting import simulator
from finpilot.forecasting.inputs import Context
from finpilot.forecasting.model import Bands, ForecastInputs, simulate
from finpilot.forecasting.simulator import Outcome, money, outcome
from finpilot.money import get_currency_registry
from finpilot.recurring.service import local_today

# 2: one-off moves into savings are no longer resampled; projected savings = income − spending
ENGINE_VERSION = 2


class MonthEnd(BaseModel):
    date: str
    p10: Decimal
    p50: Decimal
    p90: Decimal
    mean: Decimal
    partial: bool


class ScheduledOut(BaseModel):
    date: str
    label: str
    amount: Decimal
    source: str
    kind: str
    currency: str | None


class StreamOut(BaseModel):
    key: str
    label: str
    nature: str
    monthly: Decimal


class ForecastResult(BaseModel):
    id: str | None = None
    as_of: dt.date
    horizon: int
    currency: str
    data_version: int
    start_balance: Decimal
    buffer: Decimal
    dates: list[dt.date]
    p10: list[float]
    p50: list[float]
    p90: list[float]
    mean: list[float]
    outcome: Outcome
    month_ends: list[MonthEnd]
    # income minus spending per month: the change in cash plus what scheduled items move into
    # savings/investments (a SIP is saved money, as on the dashboard's savings rate)
    projected_monthly_savings: Decimal
    to_savings_monthly: Decimal = Decimal(0)  # scheduled moves into savings, per month
    unprojected_savings_monthly: Decimal = Decimal(0)  # recent one-off moves, not projected
    engine_version: int = 1  # snapshots from another version are recomputed
    low_confidence: bool
    months_of_data: float
    approximate: bool
    scheduled: list[ScheduledOut]  # upcoming items driving the forecast
    streams: list[StreamOut]  # variable spend per category (monthly expected)
    payday_multiplier: float
    fx_vol: dict[str, float]
    fx_rates: list[dict[str, Any]]
    goals: list[dict[str, Any]]
    seed: int
    n_paths: int
    duration_ms: int | None = None


def _month_ends(b: Bands) -> list[dict[str, Any]]:
    out = []
    for i, d in enumerate(b.dates):
        last = i == len(b.dates) - 1
        if last or (d + dt.timedelta(days=1)).day == 1:
            out.append(
                {
                    "date": d.isoformat(),
                    "p10": money(b.p10[i]),
                    "p50": money(b.p50[i]),
                    "p90": money(b.p90[i]),
                    "mean": money(b.mean[i]),
                    "partial": last and (d + dt.timedelta(days=1)).day != 1,
                }
            )
    return out


def to_result(
    s: Session, inp: ForecastInputs, ctx: Context, b: Bands, goals: list[dict[str, Any]]
) -> ForecastResult:
    savings_cats = {
        str(r[0])
        for r in s.execute(
            text(
                "SELECT id FROM categories WHERE flow_type = 'savings' "
                "AND (user_id IS NULL OR user_id = :u)"
            ),
            {"u": ctx.user_id},
        )
    }
    to_savings = (
        sum(-it.amount for it in inp.scheduled if it.amount < 0 and it.category_id in savings_cats)
        / inp.horizon
        * 30.44
    )
    cash_change = float((b.mean[-1] - inp.start_balance) / inp.horizon * 30.44)
    monthly_savings = cash_change + to_savings
    st = inp.streams
    streams: list[dict[str, Any]] = []
    for c, (key, label, nature) in enumerate(zip(st.keys, st.labels, st.natures, strict=True)):
        per_month = float(b.stream_mean[:, c].sum() / inp.horizon * 30.44)
        streams.append({"key": key, "label": label, "nature": nature, "monthly": money(per_month)})
    streams.sort(key=lambda x: Decimal(x["monthly"]))
    stream_exp: dict[str, list[tuple[dt.date, float]]] = {
        key: [(d, float(b.stream_mean[k, c])) for k, d in enumerate(b.dates)]
        for c, key in enumerate(st.keys)
    }
    sched_monthly: dict[str, float] = {}
    for it in inp.scheduled:
        if it.amount < 0 and it.category_id:
            sched_monthly[it.category_id] = (
                sched_monthly.get(it.category_id, 0.0) - it.amount / inp.horizon * 30.44
            )
    projections = []
    for g in goals:
        if g["status"] != "active":
            continue
        rate = ctx.rate(s, g["currency"]).rate
        projections.append(
            goals_mod.project(
                g,
                as_of=inp.as_of,
                currency=inp.currency,
                monthly_savings=monthly_savings,
                rate=rate or Decimal(1),
                stream_expected=stream_exp,
                frame_df=ctx.frame.df,
                scheduled_monthly=sched_monthly,
            )
        )
    return ForecastResult(
        as_of=inp.as_of,
        horizon=inp.horizon,
        currency=inp.currency,
        data_version=ctx.data_version,
        start_balance=money(inp.start_balance),
        buffer=money(inp.buffer),
        dates=b.dates,
        p10=[round(float(x), 2) for x in b.p10],
        p50=[round(float(x), 2) for x in b.p50],
        p90=[round(float(x), 2) for x in b.p90],
        mean=[round(float(x), 2) for x in b.mean],
        outcome=outcome(b, inp.buffer),
        month_ends=_month_ends(b),
        projected_monthly_savings=money(monthly_savings),
        to_savings_monthly=money(to_savings),
        unprojected_savings_monthly=money(ctx.unprojected_savings_monthly),
        engine_version=ENGINE_VERSION,
        low_confidence=inp.low_confidence,
        months_of_data=ctx.months_of_data,
        approximate=ctx.approximate,
        scheduled=[
            {
                "date": it.date.isoformat(),
                "label": it.label,
                "amount": money(it.amount),
                "source": it.source,
                "kind": it.kind,
                "currency": it.currency,
            }
            for it in inp.scheduled[:60]
        ],
        streams=streams,
        payday_multiplier=round(inp.payday_multiplier, 3),
        fx_vol={k: round(v, 6) for k, v in inp.fx_vol.items()},
        fx_rates=[r.as_dict(c, inp.currency) for c, r in ctx.rates.items() if r.source != "same"],
        goals=projections,
        seed=inp.seed,
        n_paths=inp.n_paths,
    )


def _persist(
    s: Session,
    uid: uuid.UUID,
    kind: str,
    inp: ForecastInputs,
    ctx: Context,
    payload: str,
    scenario: dict[str, Any] | None,
    ms: int,
) -> str:
    return str(
        s.execute(
            text("""INSERT INTO forecast_runs (user_id, kind, data_version, reporting_currency, as_of, horizon_days,
                    seed, n_paths, low_confidence, scenario_json, result_json, duration_ms)
                    VALUES (:u, :k, :dv, :cur, :as_of, :h, :seed, :n, :low, CAST(:sc AS jsonb), CAST(:r AS jsonb), :ms)
                    RETURNING id"""),
            {
                "u": uid,
                "k": kind,
                "dv": ctx.data_version,
                "cur": inp.currency,
                "as_of": inp.as_of,
                "h": inp.horizon,
                "seed": inp.seed,
                "n": inp.n_paths,
                "low": inp.low_confidence,
                "sc": json.dumps(scenario) if scenario is not None else None,
                "r": payload,
                "ms": ms,
            },
        ).scalar_one()
    )


def run_forecast(
    s: Session,
    uid: uuid.UUID,
    as_of: dt.date | None = None,
    horizon: int | None = None,
    fx: Any = None,
    persist: bool = True,
) -> ForecastResult:
    t0 = time.perf_counter()
    inp, ctx = inputs_mod.build(s, uid, as_of, horizon, fx)
    b = simulate(inp)
    res = to_result(s, inp, ctx, b, goals_mod.list_goals(s, uid))
    res.duration_ms = int((time.perf_counter() - t0) * 1000)
    if persist:
        res.id = _persist(
            s, uid, "baseline", inp, ctx, res.model_dump_json(), None, res.duration_ms
        )
    return res


def get_forecast(
    s: Session, uid: uuid.UUID, fx: Any = None, refresh: bool = False, horizon: int | None = None
) -> ForecastResult:
    from finpilot.categories.rules import thresholds

    horizon = int(horizon or thresholds()["forecast"]["horizon_days"])

    def fresh() -> ForecastResult | None:
        tz = s.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": uid}).scalar_one()
        row = s.execute(
            text("""SELECT f.id, f.result_json FROM forecast_runs f JOIN users u ON u.id = f.user_id
                    WHERE f.user_id = :u AND f.kind = 'baseline' AND f.data_version = u.data_version
                      AND f.reporting_currency = u.reporting_currency AND f.as_of = :today AND f.horizon_days = :h
                    ORDER BY f.created_at DESC LIMIT 1"""),
            {"u": uid, "today": local_today(tz), "h": horizon},
        ).first()
        if not row:
            return None
        r = ForecastResult.model_validate(row[1])
        if r.engine_version != ENGINE_VERSION:
            return None
        r.id = str(row[0])
        # the cash buffer is a setting (no data_version bump): a changed buffer means new verdicts
        buf = s.execute(
            text("""SELECT st.cash_buffer, u.reporting_currency FROM users u
                    LEFT JOIN user_settings st ON st.user_id = u.id WHERE u.id = :u"""),
            {"u": uid},
        ).one()
        want = (
            buf[0]
            if buf[0] is not None
            else get_currency_registry().get(buf[1]).thresholds.default_cash_buffer
        )
        return r if Decimal(str(r.buffer)) == Decimal(str(want)) else None

    if not refresh and (cached := fresh()) is not None:
        return cached
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('forecast:' || CAST(:u AS text)))"), {"u": uid}
    )
    if not refresh and (cached := fresh()) is not None:
        return cached
    return run_forecast(s, uid, horizon=horizon, fx=fx)


def simulate_scenario(
    s: Session,
    uid: uuid.UUID,
    scenario: Any,
    fx: Any = None,
    horizon: int | None = None,
    as_of: dt.date | None = None,
    persist: bool = True,
    with_levers: bool = True,
) -> simulator.SimulationResult:
    t0 = time.perf_counter()
    inp, ctx = inputs_mod.build(s, uid, as_of, horizon, fx)
    res = simulator.run(s, scenario, inp, ctx, with_levers=with_levers)
    if persist:
        res.forecast_run_id = _persist(
            s,
            uid,
            "scenario",
            inp,
            ctx,
            res.model_dump_json(),
            scenario.model_dump(mode="json"),
            int((time.perf_counter() - t0) * 1000),
        )
    return res


__all__ = ["ForecastResult", "get_forecast", "run_forecast", "simulate_scenario"]
