"""Recurring detection (pure): periods, regularity, extras, price history, lapsing, grouping."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from finpilot.recurring.detect import (
    Candidate,
    DetectConfig,
    Detected,
    Fit,
    Occ,
    canonical,
    detect,
    fit_series,
    match_rules,
    merge_candidates,
    monthly_equivalent,
)
from finpilot.recurring.rules import _add_months

CFG = DetectConfig.load()
D = Decimal


def monthly(
    n: int, amount: str = "649", start: dt.date = dt.date(2025, 1, 5), day: int = 5
) -> list[Occ]:
    return [Occ(f"m{i}", _add_months(start, i, day), D(amount)) for i in range(n)]


def test_monthly_series_with_weekend_shifts():
    occ = monthly(8)
    occ[2] = Occ("m2", occ[2].date + dt.timedelta(days=2), occ[2].amount)  # posted on Monday
    f = fit_series(occ, dt.date(2025, 9, 1), CFG)
    assert f and f.period == "monthly" and f.n == 8 and f.status == "active"
    assert f.next_expected == dt.date(2025, 9, 5)
    assert f.amount_typical == D("649.00") and f.amount_cv == 0


def test_biweekly_payroll_beats_monthly_subchain():
    occ = [
        Occ(f"p{i}", dt.date(2025, 1, 10) + dt.timedelta(days=14 * i), D("2650")) for i in range(10)
    ]
    f = fit_series(occ, dt.date(2025, 5, 20), CFG)
    assert f and f.period == "biweekly" and f.n == 10
    assert f.next_expected == occ[-1].date + dt.timedelta(days=14)


def test_yearly_needs_two_near_fixed_charges():
    yearly = [Occ("y0", dt.date(2024, 3, 1), D("999")), Occ("y1", dt.date(2025, 3, 2), D("999"))]
    f = fit_series(yearly, dt.date(2025, 6, 1), CFG)
    assert f and f.period == "yearly" and f.next_expected == dt.date(2026, 3, 2)
    varied = [Occ("y0", dt.date(2024, 3, 1), D("999")), Occ("y1", dt.date(2025, 3, 2), D("1500"))]
    assert fit_series(varied, dt.date(2025, 6, 1), CFG) is None


def test_duplicate_inside_series_is_set_aside():
    occ = monthly(6)
    occ.append(Occ("dup", occ[3].date + dt.timedelta(days=1), D("649")))
    f = fit_series(occ, dt.date(2025, 7, 1), CFG)
    assert f and f.period == "monthly" and f.n == 6 and f.extra_ids == ["dup"]


def test_frequent_habit_is_not_a_series():
    rng = np.random.default_rng(7)
    d, occ = dt.date(2025, 1, 1), []
    for i in range(120):  # ~8 coffees a month at random days and prices
        d += dt.timedelta(days=int(rng.integers(1, 7)))
        occ.append(Occ(f"c{i}", d, D(str(round(float(rng.lognormal(1.6, 0.3)), 2)))))
    assert fit_series(occ, d, CFG) is None


def test_irregular_amounts_are_not_bills():
    occ = [
        Occ(f"x{i}", _add_months(dt.date(2025, 1, 1), i, 3), D(a))
        for i, a in enumerate(["100", "400", "50", "900", "220"])
    ]
    assert fit_series(occ, dt.date(2025, 6, 10), CFG) is None


def test_price_hike_recorded_once_on_fixed_series_only():
    occ = monthly(5, "15.49") + [
        Occ(f"h{i}", _add_months(dt.date(2025, 6, 5), i, 5), D("19.98")) for i in range(3)
    ]
    f = fit_series(occ, dt.date(2025, 9, 1), CFG)
    assert f and len(f.hikes) == 1
    assert (
        f.hikes[0]["txn_id"] == "h0"
        and f.hikes[0]["from"] == "15.49"
        and f.hikes[0]["pct"] == 28.99
    )
    # a variable utility bill never produces "hikes"
    rng = np.random.default_rng(3)
    bills = [
        Occ(
            f"b{i}",
            _add_months(dt.date(2025, 1, 20), i, 20),
            D(str(round(120 * float(np.exp(rng.normal(0, 0.2))), 2))),
        )
        for i in range(9)
    ]
    fb = fit_series(bills, dt.date(2025, 10, 1), CFG)
    assert fb and fb.period == "monthly" and fb.hikes == []


def test_lapsed_when_charges_stop():
    f = fit_series(monthly(6), dt.date(2025, 9, 20), CFG)  # last charge 5 Jun
    assert f and f.status == "lapsed"


def test_typo_spellings_merge_but_distinct_merchants_do_not():
    c = [
        Candidate("SPOTIFY", "expense", "INR", monthly(5, "119")),
        Candidate("SPTOIFY", "expense", "INR", [Occ("t", dt.date(2025, 6, 5), D("119"))]),
        Candidate("SWIGGY INSTAMART", "expense", "INR", monthly(3, "300")),
        Candidate("SWIGGY", "expense", "INR", monthly(3, "450", day=9)),
        Candidate("SPOTIFY", "expense", "USD", monthly(3, "10")),  # other currency: separate
    ]
    merged = {(m.merchant_norm, m.currency): m for m in merge_candidates(c, CFG.merge_similarity)}
    assert len(merged[("SPOTIFY", "INR")].occ) == 6
    assert merged[("SPOTIFY", "INR")].names == {"SPOTIFY", "SPTOIFY"}
    assert ("SWIGGY", "INR") in merged and ("SWIGGY INSTAMART", "INR") in merged
    assert canonical("APRATMENTS RENT") == canonical("APARTMENTS RENT")


def test_one_merchant_two_subscriptions_split_by_amount():
    a = monthly(6, "2.99", day=3)
    b = monthly(6, "9.99", day=17)
    found = detect([Candidate("APPLE BILL", "expense", "USD", a + b)], dt.date(2025, 7, 1), CFG)
    assert sorted(d.fit.amount_typical for d in found) == [D("2.99"), D("9.99")]
    assert all(d.series_key.startswith("expense|USD|APPLE BILL|~") for d in found)


def test_declared_rule_links_by_name_or_by_amount_and_period():
    netflix = Detected(
        "k1", Candidate("NETFLIX", "expense", "INR", [], names={"NETFLIX"}), _fit("monthly", "649")
    )
    rent = Detected(
        "k2",
        Candidate("RAMESH KUMAR", "expense", "INR", [], names={"RAMESH KUMAR"}),
        _fit("monthly", "25000"),
    )
    gym = Detected(
        "k3", Candidate("GYM", "expense", "INR", [], names={"GYM"}), _fit("monthly", "1500")
    )
    other = Detected(
        "k4",
        Candidate("YOGA STUDIO", "expense", "INR", [], names={"YOGA STUDIO"}),
        _fit("monthly", "900"),
    )
    rules = [
        {
            "id": "r1",
            "kind": "expense",
            "description": "Netflix",
            "amount": D("649"),
            "currency": "INR",
            "frequency": "monthly",
            "category_id": None,
        },
        {
            "id": "r2",
            "kind": "expense",
            "description": "Rent",
            "amount": D("24800"),
            "currency": "INR",
            "frequency": "monthly",
            "category_id": None,
        },
        {
            "id": "r3",
            "kind": "expense",
            "description": "Gym",
            "amount": D("3000"),
            "currency": "INR",
            "frequency": "weekly",
            "category_id": None,
        },
    ]
    match_rules([netflix, rent, gym, other], rules)
    # same merchant name → declared wins even with another schedule; unrelated series stay suggestions
    assert (netflix.rule_id, rent.rule_id, gym.rule_id, other.rule_id) == ("r1", "r2", "r3", None)


def _fit(period: str, amount: str) -> Fit:
    d = dt.date(2025, 1, 1)
    return Fit(
        period,
        30.44,
        1.0,
        D(amount),
        D(amount),
        D(amount),
        0.0,
        d,
        d,
        d + dt.timedelta(days=30),
        "active",
        0.9,
        [],
        [],
        [],
        [],
    )


def test_monthly_equivalent():
    assert monthly_equivalent(D("120"), "yearly") == D("10.00")
    assert monthly_equivalent(D("100"), "weekly") == D("433.33")
    assert monthly_equivalent(D("2650"), "biweekly") == D("5741.67")


@settings(max_examples=60, deadline=None)
@given(
    n=st.integers(min_value=4, max_value=18),
    day=st.integers(min_value=1, max_value=28),
    shifts=st.lists(st.integers(min_value=0, max_value=3), min_size=18, max_size=18),
    amount=st.decimals(min_value=D("1"), max_value=D("100000"), places=2),
)
def test_property_monthly_with_posting_delays_is_detected(n, day, shifts, amount):
    """Any monthly charge posted 0–3 days late, ≥ 4 times, is a monthly series predicted after its last charge."""
    start = dt.date(2024, 1, day)
    occ = [
        Occ(f"o{i}", _add_months(start, i, day) + dt.timedelta(days=shifts[i]), amount)
        for i in range(n)
    ]
    f = fit_series(occ, occ[-1].date, CFG)
    assert f is not None and f.period == "monthly" and f.n == n
    assert f.next_expected > f.last_seen
    assert 26 <= (f.next_expected - f.last_seen).days <= 34
