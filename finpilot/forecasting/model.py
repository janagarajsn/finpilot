"""Cash-flow forecasting engine (pure numpy; no I/O). Floats live only in here; callers convert
results to Decimal at the boundary.

Daily flow on future day t = variable spend/income + scheduled items:

* Variable streams (one per category; scheduled and recurring-series transactions excluded):
  each path samples a HISTORICAL DAY with the same weekday and takes that day's flows for all
  streams (keeps day-of-week seasonality and cross-category correlation), scaled per stream by
  (exponentially weighted mean / plain mean) so recent behaviour counts more. A payday multiplier
  applies to discretionary streams in the window after projected paydays, compensated on the
  other days so the monthly expectation is unchanged. Scenario factors (category cuts) multiply
  a stream from a start day.
  Each path also scales its variable flows by a bootstrap re-estimate of the spending level from
  the L-day window (parameter uncertainty), so long horizons are not overconfident.
* Scheduled items (declared rules, detected series, scenario items): fixed amount, perturbed by
  a mean-preserving lognormal with the item's CV, and (P1) by an FX random walk for items in a
  foreign currency (daily σ from stored rates).

Common random numbers: every item draws from its own seed (user seed + item key) and variable
spend from a fixed stream, so a scenario that adds or removes items changes NOTHING else in any
path. Baseline and scenario differ exactly by the scenario's own effect.
"""

from __future__ import annotations

import datetime as dt
import zlib
from dataclasses import dataclass, field

import numpy as np


def stable_hash(key: str) -> int:
    return zlib.crc32(key.encode("utf-8"))


@dataclass(frozen=True)
class ScheduledItem:
    key: str  # stable per occurrence, e.g. "rule:<id>:2025-11-01"
    date: dt.date
    amount: float  # signed, reporting currency, expected value
    label: str
    source: str  # declared | detected | scenario
    kind: str = "expense"  # expense | income
    cv: float = 0.0
    currency: str | None = None  # original currency when foreign (FX perturbation applies)
    category_id: str | None = None
    ref_id: str | None = None


@dataclass
class Streams:
    """Variable flows over the lookback window: matrix C × L of signed daily amounts."""

    hist_dates: list[dt.date]
    matrix: np.ndarray
    keys: list[str]
    labels: list[str]
    natures: list[str]

    @property
    def empty(self) -> bool:
        return self.matrix.size == 0

    def scale(self, half_life: float) -> np.ndarray:
        """EW mean / plain mean per stream (1 where the plain mean is 0)."""
        if self.empty:
            return np.ones(0)
        L = self.matrix.shape[1]
        w = 0.5 ** ((L - 1 - np.arange(L)) / max(half_life, 1.0))
        w /= w.sum()
        ew = self.matrix @ w
        plain = self.matrix.mean(axis=1)
        out = np.ones(len(self.keys))
        nz = np.abs(plain) > 1e-9
        out[nz] = ew[nz] / plain[nz]
        return np.clip(out, 0.0, 5.0)


@dataclass
class ForecastInputs:
    as_of: dt.date
    horizon: int
    currency: str
    start_balance: float
    buffer: float
    streams: Streams
    scheduled: list[ScheduledItem]
    seed: int
    n_paths: int = 2000
    half_life: float = 45.0
    paydays: list[dt.date] = field(default_factory=list)
    payday_window: int = 3
    payday_multiplier: float = 1.0
    fx_vol: dict[str, float] = field(default_factory=dict)  # currency → daily σ of log rate
    factors: dict[str, tuple[dt.date, float]] = field(
        default_factory=dict
    )  # stream key → (from, ×)
    low_confidence: bool = False
    band_widen: float = 1.0

    @property
    def dates(self) -> list[dt.date]:
        return [self.as_of + dt.timedelta(days=k) for k in range(1, self.horizon + 1)]


@dataclass
class Bands:
    dates: list[dt.date]
    p10: np.ndarray
    p50: np.ndarray
    p90: np.ndarray
    mean: np.ndarray  # exact expectation (deterministic)
    min_per_path: np.ndarray  # (N,)
    end_per_path: np.ndarray  # (N,)
    flows_mean: np.ndarray  # expected daily net flow
    stream_mean: np.ndarray  # H × C expected daily flow per variable stream

    def p_below(self, level: float) -> float:
        return float((self.min_per_path < level).mean())


def _payday_mask(inp: ForecastInputs) -> np.ndarray:
    """Future days inside the window after a projected payday."""
    mask = np.zeros(inp.horizon, dtype=bool)
    for p in inp.paydays:
        k0 = (p - inp.as_of).days - 1
        for i in range(inp.payday_window):
            if 0 <= k0 + i < inp.horizon:
                mask[k0 + i] = True
    return mask


def _payday_factors(inp: ForecastInputs, expected: np.ndarray) -> np.ndarray:
    """Per-day multiplier for discretionary streams: × payday_multiplier inside payday windows and
    a compensating factor elsewhere so that EXPECTED discretionary spend over the horizon is
    unchanged (`expected` = per-day expected discretionary flow without the effect)."""
    f = np.ones(inp.horizon)
    mask = _payday_mask(inp)
    if inp.payday_multiplier == 1.0 or not mask.any() or mask.all():
        return f
    total, inside = float(expected.sum()), float(expected[mask].sum())
    outside = total - inside
    if abs(outside) < 1e-9:
        return f
    m = inp.payday_multiplier
    other = (total - m * inside) / outside
    if other <= 0:  # extreme multiplier: never let the rest of the month flip sign
        return f
    f[mask], f[~mask] = m, other
    return f


def _factor_matrix(inp: ForecastInputs, scale: np.ndarray) -> np.ndarray:
    """H × C multipliers: EW scale × payday effect (discretionary only) × scenario factors."""
    H, C = inp.horizon, len(inp.streams.keys)
    F = np.tile(scale, (H, 1)) if C else np.ones((H, 0))
    disc = [c for c, n in enumerate(inp.streams.natures) if n in ("discretionary", "unknown")]
    if disc:
        X = inp.streams.matrix.T
        weekday = np.array([d.weekday() for d in inp.streams.hist_dates])
        expected = np.array(
            [
                X[weekday == d.weekday()][:, disc].mean(axis=0) @ scale[disc]
                if (weekday == d.weekday()).any()
                else 0.0
                for d in inp.dates
            ]
        )
        pay = _payday_factors(inp, expected)
        for c in disc:
            F[:, c] *= pay
    dates = inp.dates
    for c, key in enumerate(inp.streams.keys):
        if key in inp.factors:
            start, mult = inp.factors[key]
            for k, d in enumerate(dates):
                if d >= start:
                    F[k, c] *= mult
    return F


def simulate(inp: ForecastInputs) -> Bands:
    H, N = inp.horizon, inp.n_paths
    dates = inp.dates
    flows = np.zeros((N, H))
    mean_flows = np.zeros(H)
    stream_mean = np.zeros((H, len(inp.streams.keys)))

    # ---------------------------------------------------------------- variable streams
    st = inp.streams
    if not st.empty:
        scale = st.scale(inp.half_life)
        F = _factor_matrix(inp, scale)
        X = st.matrix.T  # (L, C)
        weekday = np.array([d.weekday() for d in st.hist_dates])
        pools = {w: np.flatnonzero(weekday == w) for w in range(7)}
        all_days = np.arange(len(st.hist_dates))
        rng = np.random.default_rng(np.random.SeedSequence([inp.seed, 1]))
        var = np.zeros((N, H))
        for k, d in enumerate(dates):
            pool = pools.get(d.weekday())
            pool = pool if pool is not None and len(pool) else all_days
            idx = rng.choice(pool, size=N)
            var[:, k] = X[idx] @ F[k]
            stream_mean[k] = X[pool].mean(axis=0) * F[k]
            mean_flows[k] += stream_mean[k].sum()
        # Parameter uncertainty: each path re-estimates the spending level from a bootstrap
        # resample of the estimation window (the rate is an estimate from L days, not a fact).
        totals = X.sum(axis=1)
        level = float(totals.mean())
        if abs(level) > 1e-9:
            rng_lv = np.random.default_rng(np.random.SeedSequence([inp.seed, 4]))
            boot = rng_lv.choice(totals, size=(N, len(totals))).mean(axis=1) / level
            var *= np.clip(boot, 0.0, 3.0)[:, None]
        flows += var

    # ---------------------------------------------------------------- FX walks (P1)
    walks: dict[str, np.ndarray] = {}
    for cur, sigma in sorted(inp.fx_vol.items()):
        if sigma > 0:
            rng = np.random.default_rng(np.random.SeedSequence([inp.seed, 3, stable_hash(cur)]))
            walks[cur] = np.cumsum(rng.standard_normal((N, H)) * sigma, axis=1)

    # ---------------------------------------------------------------- scheduled items
    for it in inp.scheduled:
        k = (it.date - inp.as_of).days - 1
        if not 0 <= k < H:
            continue
        mult = np.ones(N)
        if it.cv > 0:
            rng = np.random.default_rng(np.random.SeedSequence([inp.seed, 2, stable_hash(it.key)]))
            mult *= np.exp(it.cv * rng.standard_normal(N) - it.cv**2 / 2)
        if it.currency and it.currency in walks:
            sigma = inp.fx_vol[it.currency]
            mult *= np.exp(walks[it.currency][:, k] - 0.5 * sigma**2 * (k + 1))
        flows[:, k] += it.amount * mult
        mean_flows[k] += it.amount

    balance = inp.start_balance + np.cumsum(flows, axis=1)
    p10, p50, p90 = np.percentile(balance, [10, 50, 90], axis=0)
    if inp.low_confidence and inp.band_widen > 1:
        p10 = p50 - inp.band_widen * (p50 - p10)
        p90 = p50 + inp.band_widen * (p90 - p50)
    return Bands(
        dates=dates,
        p10=p10,
        p50=p50,
        p90=p90,
        mean=inp.start_balance + np.cumsum(mean_flows),
        min_per_path=balance.min(axis=1),
        end_per_path=balance[:, -1],
        flows_mean=mean_flows,
        stream_mean=stream_mean,
    )


# ---------------------------------------------------------------------------- naive baselines
def baseline_trailing_mean(
    daily_net: np.ndarray, start: float, horizon: int, window: int = 90
) -> np.ndarray:
    """Balance path if every future day repeats the mean daily net flow of the last `window` days."""
    rate = float(np.mean(daily_net[-window:])) if len(daily_net) else 0.0
    return start + rate * np.arange(1, horizon + 1)


def baseline_last_month(
    hist: dict[dt.date, float], as_of: dt.date, start: float, horizon: int
) -> np.ndarray:
    """Balance path that replays last full calendar month's daily flows by day of month."""
    first_this = as_of.replace(day=1)
    last_end = first_this - dt.timedelta(days=1)
    last_start = last_end.replace(day=1)
    by_dom = {d.day: v for d, v in hist.items() if last_start <= d <= last_end}
    flows = [by_dom.get((as_of + dt.timedelta(days=k)).day, 0.0) for k in range(1, horizon + 1)]
    return start + np.cumsum(flows)
