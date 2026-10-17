"""Analyzer input: one pandas frame per user (reporting currency) + context shared by detectors.

Floats live only inside pandas/numpy; detectors convert back with `money()` at the boundary.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.db.sql_loader import sql
from finpilot.money import CurrencyThresholds, get_currency_registry, quantize, to_decimal

HISTORY_YEARS = 3
EXCLUDED_FLOWS = ("transfer", "savings")  # moving money is not spending
EMPTY_COLUMNS = [
    "id", "date", "raw_description", "merchant_norm", "amount", "currency", "amount_reporting",
    "reporting_currency", "category_id", "recurring_rule_id", "source", "category_slug",
    "category_name", "flow_type", "nature", "amt", "orig",
]  # fmt: skip


@dataclass
class Frame:
    user_id: uuid.UUID
    df: pd.DataFrame  # all transactions ≤ as_of with a reporting amount
    monthly: pd.DataFrame  # category × month spend (zero-filled from the first month with data)
    detected: list[dict[str, Any]]  # detected_recurrences rows (status <> 'gone')
    as_of: dt.date
    scan_start: dt.date  # findings about [scan_start, as_of]
    currency: str  # reporting currency
    cfg: dict[str, Any]  # thresholds.yml
    missing_rate_rows: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def thr(self) -> CurrencyThresholds:
        return get_currency_registry().get(self.currency).thresholds

    @property
    def history_start(self) -> dt.date | None:
        return None if self.df.empty else self.df["date"].min().date()

    def money(self, x: float | Decimal, currency: str | None = None) -> Decimal:
        return quantize(to_decimal(round(float(x), 2)), currency or self.currency)

    def spending(self) -> pd.DataFrame:
        """Expenses that count as spend (no transfers/savings), with positive `spend`."""
        d = self.df
        return d[(d["amt"] < 0) & (~d["flow_type"].isin(EXCLUDED_FLOWS))]

    def window(self, days: int) -> pd.DataFrame:
        start = pd.Timestamp(self.as_of - dt.timedelta(days=days - 1))
        return self.df[self.df["date"] >= start]


def load_frame(
    s: Session,
    user_id: uuid.UUID,
    as_of: dt.date,
    currency: str,
    cfg: dict[str, Any],
    lookback_days: int | None,
) -> Frame:
    start = as_of - dt.timedelta(days=366 * HISTORY_YEARS)
    rows = (
        s.execute(sql("analysis_txns"), {"u": user_id, "start": start, "as_of": as_of})
        .mappings()
        .all()
    )
    df = pd.DataFrame([dict(r) for r in rows])
    missing = 0
    if df.empty:
        df = pd.DataFrame(columns=EMPTY_COLUMNS)
        df["date"] = pd.to_datetime(df["date"])
    else:
        missing = int(df["amount_reporting"].isna().sum())
        df = df[df["amount_reporting"].notna()].copy()
        df["id"] = df["id"].astype(str)
        df["category_id"] = df["category_id"].map(lambda v: str(v) if v is not None else None)
        df["recurring_rule_id"] = df["recurring_rule_id"].map(
            lambda v: str(v) if v is not None else None
        )
        df["date"] = pd.to_datetime(df["date"])
        df["amt"] = df["amount_reporting"].astype(float)
        df["orig"] = df["amount"].astype(float)
        df["merchant_norm"] = df["merchant_norm"].fillna("")
        df = df.reset_index(drop=True)
    first = df["date"].min().date() if len(df) else as_of
    monthly = pd.DataFrame(
        [
            dict(r)
            for r in s.execute(
                sql("category_monthly"), {"u": user_id, "start": first, "as_of": as_of}
            ).mappings()
        ]
    )
    if not monthly.empty:
        monthly["spend"] = monthly["spend"].astype(float)
        monthly["category_id"] = monthly["category_id"].map(
            lambda v: str(v) if v is not None else None
        )
    detected = [
        dict(r)
        for r in s.execute(
            text("""SELECT d.*, c.name AS category_name, c.slug AS category_slug, c.nature AS category_nature
                    FROM detected_recurrences d LEFT JOIN categories c ON c.id = d.category_id
                    WHERE d.user_id = :u AND d.status <> 'gone' ORDER BY d.series_key"""),
            {"u": user_id},
        ).mappings()
    ]
    scan_start = (
        first if lookback_days is None else max(first, as_of - dt.timedelta(days=lookback_days - 1))
    )
    return Frame(user_id, df, monthly, detected, as_of, scan_start, currency, cfg, missing)


def robust_z(x: float, values: np.ndarray, mad_floor: float) -> tuple[float, float, float]:
    """(z, median, MAD) with 0.6745·(x − median)/max(MAD, floor) — Iglewicz & Hoaglin."""
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    return 0.6745 * (x - med) / max(mad, mad_floor), med, mad


def cross_rate(fr: Frame, txn_ids: list[str]) -> float:
    """Reporting-currency units per original unit, from the latest of the given transactions."""
    rows = fr.df[fr.df["id"].isin(txn_ids)]
    if rows.empty:
        return 1.0
    last = rows.sort_values("date").iloc[-1]
    return abs(float(last["amt"]) / float(last["orig"])) if float(last["orig"]) else 1.0
