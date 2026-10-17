"""Forecast engine + scenario properties (pure; no DB). Property tests per the spec."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from finpilot.forecasting.inputs import ForecastError, Rate
from finpilot.forecasting.model import (
    ForecastInputs,
    ScheduledItem,
    Streams,
    baseline_last_month,
    baseline_trailing_mean,
    simulate,
)
from finpilot.forecasting.scenarios import Applier, installment_payment, parse
from finpilot.forecasting.simulator import outcome, verdict

AS_OF = dt.date(2025, 6, 30)


def make_inputs(
    seed: int = 7, n: int = 400, start: float = 50_000.0, low: bool = False
) -> ForecastInputs:
    rng = np.random.default_rng(0)
    L = 120
    hist = [AS_OF - dt.timedelta(days=L - 1 - i) for i in range(L)]
    weekend = np.array([d.weekday() >= 5 for d in hist])
    groceries = -rng.gamma(2.0, 400, L)
    dining = -rng.gamma(1.5, 300, L) * np.where(weekend, 2.0, 1.0) * (rng.random(L) < 0.5)
    refunds = rng.gamma(1.0, 200, L) * (rng.random(L) < 0.05)
    streams = Streams(
        hist,
        np.vstack([groceries, dining, refunds]),
        ["g", "d", "uncat:in"],
        ["Groceries", "Dining Out", "Uncategorized income"],
        ["essential_variable", "discretionary", "unknown"],
    )
    items = []
    for m in range(1, 4):
        pay = dt.date(2025, 6 + m, 1)
        items += [
            ScheduledItem(f"rule:sal:{pay}", pay, 90_000, "Salary", "declared", "income"),
            ScheduledItem(
                f"rule:rent:{pay}", pay + dt.timedelta(days=2), -30_000, "Rent", "declared"
            ),
            ScheduledItem(
                f"det:netflix:{pay}",
                pay + dt.timedelta(days=11),
                -649,
                "Netflix",
                "detected",
                ref_id="netflix",
            ),
            ScheduledItem(
                f"det:power:{pay}", pay + dt.timedelta(days=19), -1800, "Power", "detected", cv=0.2
            ),
        ]
    return ForecastInputs(
        as_of=AS_OF,
        horizon=90,
        currency="INR",
        start_balance=start,
        buffer=10_000.0,
        streams=streams,
        scheduled=items,
        seed=seed,
        n_paths=n,
        paydays=[i.date for i in items if i.kind == "income"],
        payday_multiplier=1.3,
        low_confidence=low,
        band_widen=1.5,
    )


def ctx_stub():
    return SimpleNamespace(
        currency="INR",
        detected=[
            {
                "id": "netflix",
                "kind": "expense",
                "status": "active",
                "merchant_norm": "NETFLIX",
                "amount_typical": Decimal("649"),
                "promoted_to_rule_id": None,
                "dismissed": False,
            }
        ],
        streams_by_category={"g": "g", "groceries": "g", "d": "d", "dining out": "d"},
        rate=lambda s, cur: (
            Rate(Decimal("83.50"), dt.date(2025, 6, 27), "stored")
            if cur == "USD"
            else Rate(Decimal(1), None, "same")
        ),
    )


def run(inp, sc):
    applier = Applier(None, inp, ctx_stub())  # type: ignore[arg-type]
    return simulate(applier.apply(parse(sc))), applier


def test_bands_are_ordered_and_seed_reproducible():
    a, b = simulate(make_inputs()), simulate(make_inputs())
    assert np.array_equal(a.p50, b.p50) and np.array_equal(a.min_per_path, b.min_per_path)
    assert (a.p10 <= a.p50).all() and (a.p50 <= a.p90).all()
    c = simulate(make_inputs(seed=8))
    assert not np.array_equal(a.p50, c.p50)
    k = (dt.date(2025, 7, 1) - AS_OF).days - 1
    assert a.flows_mean[k] > 80_000  # salary day: +90,000 minus that day's expected spend


def test_payday_effect_preserves_expected_spend():
    base = make_inputs()
    flat = make_inputs()
    flat.payday_multiplier = 1.0
    assert simulate(base).mean[-1] == pytest.approx(simulate(flat).mean[-1], rel=1e-9)


def test_sparse_data_widens_bands():
    tight, wide = simulate(make_inputs()), simulate(make_inputs(low=True))
    assert np.array_equal(tight.p50, wide.p50)
    assert ((wide.p90 - wide.p10) >= (tight.p90 - tight.p10) - 1e-9).all()


def test_empty_history_still_forecasts_scheduled_items():
    inp = make_inputs()
    inp.streams = Streams([], np.zeros((0, 0)), [], [], [])
    b = simulate(inp)
    assert np.isfinite(b.p50).all()
    assert b.mean[-1] == pytest.approx(50_000 + sum(i.amount for i in inp.scheduled))
    assert (
        b.p10[10] == b.p90[10]
    )  # before the first variable-amount bill (20 Jul), no spread at all


def test_verdict_rule():
    inp = make_inputs(start=200_000)
    assert verdict(simulate(inp), inp.buffer) == "affordable"
    b, _ = run(inp, {"type": "one_time_purchase", "amount": "400000"})
    assert verdict(b, inp.buffer) == "not_affordable"
    o = outcome(b, inp.buffer)
    assert o.p_below_zero > 0.99 and o.first_below_buffer_p10 is not None


def test_installments_and_fx_conversion_are_reported():
    assert installment_payment(Decimal("120000"), 12, 0) == Decimal("10000.00")
    assert installment_payment(Decimal("100000"), 12, 12) == Decimal(
        "8884.88"
    )  # standard amortization
    _, ap = run(make_inputs(), {"type": "one_time_purchase", "amount": "1200", "currency": "USD"})
    assert ap.applied[0]["amount"] == 100_200.0
    assert ap.fx_used == [
        {
            "currency": "USD",
            "reporting_currency": "INR",
            "rate": "83.5000",
            "rate_date": "2025-06-27",
            "source": "stored",
        }
    ]


def test_scenario_validation():
    with pytest.raises(ForecastError, match="after"):
        run(make_inputs(), {"type": "one_time_purchase", "amount": "10", "date": "2025-06-01"})
    with pytest.raises(ForecastError, match="horizon"):
        run(make_inputs(), {"type": "one_time_purchase", "amount": "10", "date": "2026-06-01"})
    with pytest.raises(ForecastError, match="not found"):
        run(make_inputs(), {"type": "cancel_subscription", "merchant": "SPOTIFY"})
    with pytest.raises(ForecastError, match="No recent spending"):
        run(make_inputs(), {"type": "category_cut", "category": "Travel", "pct": 50})
    with pytest.raises(Exception):  # schema: amount must be positive
        parse({"type": "one_time_purchase", "amount": "-5"})


# ---------------------------------------------------------------------------- properties (spec)
DAYS = st.integers(min_value=1, max_value=90)
AMOUNTS = st.decimals(min_value=Decimal("1"), max_value=Decimal("500000"), places=2)


@settings(max_examples=40, deadline=None)
@given(amount=AMOUNTS, day=DAYS, seed=st.integers(0, 2**31))
def test_property_purchase_never_raises_and_equals_purchase(amount, day, seed):
    inp = make_inputs(seed=seed, n=200)
    base = simulate(inp)
    b, _ = run(
        inp,
        {
            "type": "one_time_purchase",
            "amount": str(amount),
            "date": str(AS_OF + dt.timedelta(days=day)),
        },
    )
    a = float(amount)
    assert (b.end_per_path <= base.end_per_path + 1e-6).all()  # never raises any path's end balance
    assert np.allclose(
        base.end_per_path - b.end_per_path, a
    )  # common random numbers: exactly the purchase
    assert base.mean[-1] - b.mean[-1] == pytest.approx(
        a
    )  # delta = purchase at the deterministic mean


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 2**31), from_day=st.integers(1, 60))
def test_property_cancel_subscription_raises_by_monthly_times_occurrences(seed, from_day):
    inp = make_inputs(seed=seed, n=200)
    start = AS_OF + dt.timedelta(days=from_day)
    occurrences = sum(1 for i in inp.scheduled if i.ref_id == "netflix" and i.date >= start)
    base = simulate(inp)
    if occurrences == 0:
        with pytest.raises(ForecastError):
            run(
                inp, {"type": "cancel_subscription", "merchant": "netflix", "from_date": str(start)}
            )
        return
    b, _ = run(inp, {"type": "cancel_subscription", "merchant": "netflix", "from_date": str(start)})
    assert np.allclose(b.end_per_path - base.end_per_path, 649 * occurrences)


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(0, 2**31), amount=AMOUNTS, pct=st.floats(min_value=1, max_value=100), day=DAYS
)
def test_property_composite_is_sum_of_parts(seed, amount, pct, day):
    inp = make_inputs(seed=seed, n=200)
    base = simulate(inp).mean[-1]
    purchase = {
        "type": "one_time_purchase",
        "amount": str(amount),
        "date": str(AS_OF + dt.timedelta(days=day)),
    }
    cut = {"type": "category_cut", "category": "Dining Out", "pct": pct}
    loan = {"type": "loan_or_installments", "total": "60000", "n": 6, "apr": 10}
    parts = [
        simulate(Applier(None, inp, ctx_stub()).apply(parse(p))).mean[-1] - base
        for p in (purchase, cut, loan)
    ]  # type: ignore[arg-type]
    both, _ = run(inp, {"type": "composite", "parts": [purchase, cut, loan]})
    assert both.mean[-1] - base == pytest.approx(sum(parts), rel=1e-9, abs=1e-6)


@settings(max_examples=15, deadline=None)
@given(seed=st.integers(0, 2**31))
def test_property_same_seed_identical(seed):
    a, b = simulate(make_inputs(seed=seed, n=150)), simulate(make_inputs(seed=seed, n=150))
    assert np.array_equal(a.p10, b.p10) and np.array_equal(a.end_per_path, b.end_per_path)


def test_naive_baselines():
    assert list(baseline_trailing_mean(np.array([-10.0] * 90), 100.0, 3)) == [90.0, 80.0, 70.0]
    hist = {dt.date(2025, 5, d): -100.0 for d in range(1, 32)} | {dt.date(2025, 5, 1): 5000.0}
    path = baseline_last_month(
        hist, AS_OF, 0.0, 2
    )  # Jul 1 replays May 1 (+5000), Jul 2 replays May 2
    assert list(path) == [5000.0, 4900.0]
