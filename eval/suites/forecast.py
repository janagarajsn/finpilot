"""Forecast backtest suite (deterministic: identical in offline and full mode).

Per persona, ALL 15 months are loaded (ground-truth categories, so the forecaster is judged
apart from the categorizer). Rolling origins at the end of history months 9–12: at each origin
the forecaster sees only transactions up to that day (detection is re-run as of the origin) and
projects 90 days; the realized balance path comes from the transactions after the origin.

Metrics (pooled over personas × origins):
  nmae_<h>         |p50 − actual| at day h ÷ the persona's average monthly spend before the origin
                   (scale-free across INR/USD), for h in config forecast.backtest_horizons
  coverage_<h>     share of (persona, origin) with actual at day h inside p10–p90
  coverage_daily   share of all forecast days with the actual balance inside p10–p90 (target ≈ 0.80)
  skill_vs_trailing_mean / skill_vs_last_month
                   1 − MAE(model) / MAE(baseline), averaged over all horizon days (> 0 = better)
  sparse_ok        a 6-week-old account gets a low-confidence forecast with wider bands, not an error
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import statistics
from collections import defaultdict
from typing import Any

import numpy as np
from sqlalchemy import text

from eval import loader
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import _add_months, generate
from eval.synth.personas import PERSONAS
from finpilot.categories.rules import thresholds
from finpilot.db.session import user_session
from finpilot.forecasting import inputs as inputs_mod
from finpilot.forecasting.model import (
    baseline_last_month,
    baseline_trailing_mean,
    simulate,
    stable_hash,
)

log = logging.getLogger(__name__)
SUITE = "forecast"
ORIGIN_MONTHS = (9, 10, 11, 12)  # end of these history months (1-based)
HORIZON = 90


def _daily(s: Any, uid: Any) -> dict[dt.date, float]:
    rows = s.execute(
        text(
            "SELECT date, sum(amount_reporting) FROM v_transactions_reporting WHERE user_id = :u GROUP BY date"
        ),
        {"u": uid},
    ).all()
    return {r[0]: float(r[1]) for r in rows}


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    horizons = [int(h) for h in thresholds()["forecast"]["backtest_horizons"]]
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas]
    err: dict[str, dict[int, list[float]]] = {
        k: defaultdict(list) for k in ("model", "trailing", "last_month")
    }
    abs_all: dict[str, list[float]] = defaultdict(list)  # all horizon days, normalized
    cover_h: dict[int, list[bool]] = defaultdict(list)
    cover_days: list[float] = []
    per_persona: list[dict[str, Any]] = []
    chart: dict[str, Any] = {}
    for p in personas:
        ds = generate(p)
        ld = loader.load(ds, ctx.run_tag, txns=ds.txns)
        loader.apply_truth_categories(ds, ld)
        pp: dict[str, list[float]] = defaultdict(list)
        with user_session(ld.user_id) as s:
            daily = _daily(s, ld.user_id)
            for m in ORIGIN_MONTHS:
                origin = _add_months(p.start, m) - dt.timedelta(days=1)
                if origin + dt.timedelta(days=HORIZON) > ds.end:
                    continue
                inp, fctx = inputs_mod.build(
                    s,
                    ld.user_id,
                    as_of=origin,
                    horizon=HORIZON,
                    seed=stable_hash(f"{p.name}:{origin}"),
                )
                b = simulate(inp)
                start = inp.start_balance
                actual = start + np.cumsum(
                    [daily.get(origin + dt.timedelta(days=k), 0.0) for k in range(1, HORIZON + 1)]
                )
                hist = {d: v for d, v in daily.items() if d <= origin}
                dn = np.array(
                    [hist.get(origin - dt.timedelta(days=k), 0.0) for k in range(179, -1, -1)]
                )
                bl_t = baseline_trailing_mean(dn, start, HORIZON)
                bl_m = baseline_last_month(hist, origin, start, HORIZON)
                spend = -sum(
                    v for d, v in hist.items() if v < 0 and d > origin - dt.timedelta(days=90)
                ) / (90 / 30.44)
                scale = max(spend, 1.0)
                for name, path in (("model", b.p50), ("trailing", bl_t), ("last_month", bl_m)):
                    e = np.abs(path - actual) / scale
                    abs_all[name] += e.tolist()
                    for h in horizons:
                        err[name][h].append(float(e[h - 1]))
                inside = (actual >= b.p10) & (actual <= b.p90)
                cover_days.append(float(inside.mean()))
                pp["coverage"].append(float(inside.mean()))
                pp["nmae_90"].append(float(abs(b.p50[HORIZON - 1] - actual[HORIZON - 1]) / scale))
                for h in horizons:
                    cover_h[h].append(bool(inside[h - 1]))
                    res.cases.append(
                        Case(
                            case_id=f"{p.name}:{origin.isoformat()}:{h}",
                            input={
                                "persona": p.name,
                                "origin": origin.isoformat(),
                                "horizon": h,
                                "months_of_history": fctx.months_of_data,
                            },
                            expected={"balance": round(float(actual[h - 1]), 2)},
                            actual={
                                "p10": round(float(b.p10[h - 1]), 2),
                                "p50": round(float(b.p50[h - 1]), 2),
                                "p90": round(float(b.p90[h - 1]), 2),
                                "trailing_mean": round(float(bl_t[h - 1]), 2),
                                "last_month": round(float(bl_m[h - 1]), 2),
                            },
                            passed=bool(inside[h - 1]),
                            score=round(
                                1 - min(float(abs(b.p50[h - 1] - actual[h - 1]) / scale), 1.0), 4
                            ),
                            error_type=None
                            if inside[h - 1]
                            else ("above_band" if actual[h - 1] > b.p90[h - 1] else "below_band"),
                        )
                    )
                if m == ORIGIN_MONTHS[-1] and not chart:
                    chart = {
                        "persona": p.name,
                        "origin": origin.isoformat(),
                        "dates": [d.isoformat() for d in b.dates],
                        "actual": [round(float(x), 2) for x in actual],
                        "p10": [round(float(x), 2) for x in b.p10],
                        "p50": [round(float(x), 2) for x in b.p50],
                        "p90": [round(float(x), 2) for x in b.p90],
                        "trailing_mean": [round(float(x), 2) for x in bl_t],
                        "last_month": [round(float(x), 2) for x in bl_m],
                    }
        per_persona.append(
            {
                "persona": p.name,
                "currency": p.reporting_currency,
                "coverage_daily": round(statistics.mean(pp["coverage"]), 4)
                if pp["coverage"]
                else None,
                "nmae_90": round(statistics.mean(pp["nmae_90"]), 4) if pp["nmae_90"] else None,
            }
        )
        loader.cleanup(ctx.run_tag)

    sparse_ok = _sparse_check(ctx)

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    for h in horizons:
        add(f"nmae_{h}", statistics.mean(err["model"][h]), "ratio", scope="overall", primary=True)
        add(
            f"coverage_{h}",
            sum(cover_h[h]) / max(len(cover_h[h]), 1),
            "ratio",
            scope="overall",
            primary=True,
        )
        for name in ("trailing", "last_month"):
            add(
                f"nmae_{h}", statistics.mean(err[name][h]), "ratio", scope="baseline", baseline=name
            )
    add(
        "coverage_daily",
        statistics.mean(cover_days) if cover_days else 0.0,
        "ratio",
        scope="overall",
        primary=True,
    )
    m_all = statistics.mean(abs_all["model"])
    add(
        "skill_vs_trailing_mean",
        1 - m_all / statistics.mean(abs_all["trailing"]),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "skill_vs_last_month",
        1 - m_all / statistics.mean(abs_all["last_month"]),
        "ratio",
        scope="overall",
        primary=True,
    )
    add("sparse_ok", 1.0 if sparse_ok["ok"] else 0.0, "ratio", scope="overall", primary=True)
    for row in per_persona:
        if row["coverage_daily"] is not None:
            add(
                "coverage_daily",
                row["coverage_daily"],
                "ratio",
                scope="persona",
                persona=row["persona"],
            )
    res.details = {
        "per_persona": per_persona,
        "per_horizon": [
            {
                "horizon": h,
                "model": round(statistics.mean(err["model"][h]), 4),
                "trailing_mean": round(statistics.mean(err["trailing"][h]), 4),
                "last_month": round(statistics.mean(err["last_month"][h]), 4),
                "coverage": round(sum(cover_h[h]) / max(len(cover_h[h]), 1), 4),
            }
            for h in horizons
        ],
        "chart": chart,
        "sparse": sparse_ok,
        "origins": len(cover_days),
    }
    return res


def _sparse_check(ctx: SuiteContext) -> dict[str, Any]:
    """Six weeks of data → low-confidence flag, finite bands wider than the full-history ones."""
    p = next(x for x in PERSONAS if not ctx.personas or x.name in ctx.personas)
    ds = generate(dataclasses.replace(p, name=f"{p.name}__sparse"))
    cut = p.start + dt.timedelta(days=42)
    ld = loader.load(ds, ctx.run_tag, txns=[t for t in ds.txns if t.date <= cut])
    loader.apply_truth_categories(ds, ld)
    try:
        with user_session(ld.user_id) as s:
            inp, _ = inputs_mod.build(
                s, ld.user_id, as_of=cut, horizon=HORIZON, seed=stable_hash(f"{p.name}:sparse")
            )
            b = simulate(inp)
        width = float(np.mean(b.p90 - b.p10))
        ok = inp.low_confidence and bool(np.isfinite(b.p50).all()) and width > 0
        return {
            "ok": ok,
            "low_confidence": inp.low_confidence,
            "mean_band_width": round(width, 2),
            "days_of_history": 42,
        }
    finally:
        loader.cleanup(ctx.run_tag)
