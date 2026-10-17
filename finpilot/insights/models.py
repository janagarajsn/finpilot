"""Typed analyzer output. Every number here is computed by code; text is rendered from
templates/messages/findings.yml (the LLM never produces these numbers)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "low", "medium", "high"]
Kind = Literal["anomaly", "pattern", "subscription", "fx_exposure", "trend", "forecast", "goal"]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Txn-level anomaly types (scored against injected ground truth in eval/suites/anomalies.py).
TXN_ANOMALY_TYPES = (
    "duplicate_charge",
    "merchant_outlier",
    "category_outlier",
    "first_time_large",
    "price_hike",
)


class Finding(BaseModel):
    """One analyzer finding: what was measured, against which baseline, which threshold it crossed."""

    fingerprint: str  # stable across runs: type + subject + evidence
    kind: Kind
    type: str
    severity: Severity
    metric: str | None = None
    baseline: Decimal | None = None
    observed: Decimal | None = None
    threshold: Decimal | None = None
    unit: str | None = None  # currency | ratio | pct | count | z
    currency: str | None = None
    evidence_txn_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    title: str = ""
    body: str = ""
    id: str | None = None  # analysis_findings.id once persisted

    @property
    def sort_key(self) -> tuple[int, float]:
        size = (
            float(abs(self.observed or Decimal(0)) - abs(self.baseline or Decimal(0)))
            if self.unit == "currency"
            else 0.0
        )
        return SEVERITY_ORDER[self.severity], -size


class Subscription(BaseModel):
    id: str  # detected_recurrences.id
    merchant: str
    description: str
    category_id: str | None = None
    category_name: str | None = None
    period: str
    amount: Decimal  # typical charge, original currency
    currency: str
    monthly_cost: Decimal  # reporting currency
    annual_cost: Decimal  # reporting currency
    reporting_currency: str
    n_charges: int
    first_seen: dt.date
    last_seen: dt.date
    next_expected: dt.date
    status: str  # active | lapsed
    usage_rating: int | None = None
    flags: list[str] = Field(default_factory=list)  # overlap | price_hike | low_value_proxy
    overlap_group: str | None = None
    overlap_with: list[str] = Field(default_factory=list)
    low_value_basis: str | None = None  # user_rating | heuristic
    price_history: list[dict[str, Any]] = Field(default_factory=list)
    declared_rule_id: str | None = None
    evidence_txn_ids: list[str] = Field(default_factory=list)


class CategoryTrend(BaseModel):
    category_id: str | None
    name: str
    nature: str | None
    months: list[dict[str, Any]]  # [{month, spend}] zero-filled, reporting currency
    last_full_month: Decimal
    previous_month: Decimal
    trailing3_avg: Decimal
    mom_pct: float | None
    vs_trailing3_pct: float | None
    month_to_date: Decimal


class AnalysisReport(BaseModel):
    user_id: str
    as_of: dt.date
    reporting_currency: str
    data_version: int
    lookback_start: dt.date
    history_start: dt.date | None
    months_of_data: float
    findings: list[Finding] = Field(default_factory=list)
    subscriptions: list[Subscription] = Field(default_factory=list)
    subscription_totals: dict[str, Any] = Field(default_factory=dict)
    category_trends: list[CategoryTrend] = Field(default_factory=list)
    weekly_spend: list[dict[str, Any]] = Field(default_factory=list)
    patterns: dict[str, Any] = Field(default_factory=dict)
    fx_exposure: dict[str, Any] | None = None
    recurring: list[dict[str, Any]] = Field(default_factory=list)  # detected series summary
    sparse: bool = False  # < 2 months of data: findings are limited
    duration_ms: int | None = None

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def top(self, kind: str | None = None, n: int = 3) -> list[Finding]:
        pool = [f for f in self.findings if kind is None or f.kind == kind]
        return sorted(pool, key=lambda f: f.sort_key)[:n]
