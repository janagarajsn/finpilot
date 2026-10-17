"""Pure schedule math (no DB)."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finpilot.recurring.rules import Schedule, next_n, next_occurrence, occurrences_between

D = dt.date


def test_monthly_31st_clamps_without_drift():
    s = Schedule("monthly", D(2024, 1, 31))
    assert next_n(s, D(2024, 1, 1), 5) == [
        D(2024, 1, 31),
        D(2024, 2, 29),
        D(2024, 3, 31),
        D(2024, 4, 30),
        D(2024, 5, 31),
    ]
    assert next_n(Schedule("monthly", D(2023, 1, 31)), D(2023, 2, 1), 1) == [D(2023, 2, 28)]


def test_explicit_day_of_month_and_interval():
    s = Schedule("monthly", D(2024, 1, 10), interval=2, day_of_month=5)
    assert next_n(s, D(2024, 1, 1), 3) == [D(2024, 2, 5), D(2024, 4, 5), D(2024, 6, 5)]


def test_quarterly_and_yearly_leap_day():
    assert next_n(Schedule("quarterly", D(2024, 1, 15)), D(2024, 1, 1), 3) == [
        D(2024, 1, 15),
        D(2024, 4, 15),
        D(2024, 7, 15),
    ]
    assert next_n(Schedule("yearly", D(2024, 2, 29)), D(2024, 1, 1), 3) == [
        D(2024, 2, 29),
        D(2025, 2, 28),
        D(2026, 2, 28),
    ]


def test_weekly_biweekly_align_to_weekday():
    s = Schedule("weekly", D(2024, 1, 3), weekday=0)  # start Wed → first Monday after
    assert next_n(s, D(2024, 1, 1), 2) == [D(2024, 1, 8), D(2024, 1, 15)]
    assert next_n(Schedule("biweekly", D(2024, 1, 5)), D(2024, 1, 1), 3) == [
        D(2024, 1, 5),
        D(2024, 1, 19),
        D(2024, 2, 2),
    ]


def test_end_date_and_skip_dates():
    s = Schedule(
        "daily", D(2024, 1, 1), end_date=D(2024, 1, 5), skip_dates=frozenset({D(2024, 1, 3)})
    )
    assert occurrences_between(s, D(2024, 1, 1), D(2024, 12, 31)) == [
        D(2024, 1, 1),
        D(2024, 1, 2),
        D(2024, 1, 4),
        D(2024, 1, 5),
    ]
    assert next_occurrence(s, D(2024, 1, 6)) is None


def test_invalid_schedules_rejected():
    with pytest.raises(ValueError):
        Schedule("monthly", D(2024, 1, 1), end_date=D(2023, 1, 1))
    with pytest.raises(ValueError):
        Schedule("monthly", D(2024, 1, 1), day_of_month=32)


@given(
    st.dates(min_value=D(2000, 1, 1), max_value=D(2040, 1, 1)),
    st.sampled_from(["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"]),
    st.integers(min_value=1, max_value=4),
)
def test_occurrences_strictly_increasing_and_on_or_after_start(start, freq, interval):
    occ = next_n(Schedule(freq, start, interval=interval), start, 12)
    assert len(occ) == 12 and occ[0] >= start
    assert all(a < b for a, b in itertools.pairwise(occ))
