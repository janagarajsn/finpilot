"""Pure schedule math for user-declared recurring rules (no I/O).

Occurrence k is computed from the ANCHOR (start date aligned to day_of_month / weekday), never
from the previous occurrence, so month-end clamping does not drift:
a monthly rule on the 31st yields Jan 31, Feb 29 (leap) / Feb 28, Mar 31, Apr 30, ...
Yearly rules anchored on Feb 29 fall on Feb 28 in non-leap years.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

Frequency = Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"]
MAX_OCCURRENCES = 10_000  # safety bound for iteration


@dataclass(frozen=True)
class Schedule:
    frequency: Frequency
    start_date: dt.date
    interval: int = 1
    day_of_month: int | None = None  # monthly/quarterly/yearly; defaults to start_date.day
    weekday: int | None = None  # weekly/biweekly, 0 = Monday; defaults to start_date.weekday()
    end_date: dt.date | None = None
    skip_dates: frozenset[dt.date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.interval < 1:
            raise ValueError("interval must be >= 1")
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date before start_date")
        if self.day_of_month is not None and not 1 <= self.day_of_month <= 31:
            raise ValueError("day_of_month must be 1..31")
        if self.weekday is not None and not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be 0..6")


def _add_months(d: dt.date, months: int, day: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _anchor(s: Schedule) -> dt.date:
    """First occurrence on/after start_date honouring day_of_month / weekday."""
    if s.frequency in ("weekly", "biweekly"):
        wd = s.start_date.weekday() if s.weekday is None else s.weekday
        return s.start_date + dt.timedelta(days=(wd - s.start_date.weekday()) % 7)
    if s.frequency in ("monthly", "quarterly", "yearly") and s.day_of_month is not None:
        cand = _add_months(s.start_date, 0, s.day_of_month)
        if cand < s.start_date:
            cand = _add_months(s.start_date, 1, s.day_of_month)
        return cand
    return s.start_date


def _nth(s: Schedule, anchor: dt.date, k: int) -> dt.date:
    if s.frequency == "daily":
        return anchor + dt.timedelta(days=k * s.interval)
    if s.frequency == "weekly":
        return anchor + dt.timedelta(weeks=k * s.interval)
    if s.frequency == "biweekly":
        return anchor + dt.timedelta(weeks=2 * k * s.interval)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[s.frequency] * s.interval * k
    return _add_months(anchor, months, s.day_of_month or anchor.day)


def iter_occurrences(s: Schedule, *, include_skipped: bool = False) -> Iterator[dt.date]:
    anchor = _anchor(s)
    for k in range(MAX_OCCURRENCES):
        d = _nth(s, anchor, k)
        if s.end_date and d > s.end_date:
            return
        if include_skipped or d not in s.skip_dates:
            yield d


def occurrences_between(s: Schedule, start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    for d in iter_occurrences(s):
        if d > end:
            break
        if d >= start:
            out.append(d)
    return out


def next_occurrence(s: Schedule, on_or_after: dt.date) -> dt.date | None:
    for d in iter_occurrences(s):
        if d >= on_or_after:
            return d
    return None


def next_n(s: Schedule, on_or_after: dt.date, n: int = 5) -> list[dt.date]:
    out: list[dt.date] = []
    for d in iter_occurrences(s):
        if d >= on_or_after:
            out.append(d)
            if len(out) == n:
                break
    return out
