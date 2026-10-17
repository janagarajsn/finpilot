"""Request/response models (OpenAPI). Money/rates travel as strings to keep Decimal precision."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    name: str = Field(min_length=1, max_length=100)
    reporting_currency: str = Field(default="INR", min_length=3, max_length=3)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str | None = None


class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    new_password: str = Field(max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — OAuth token type, not a secret
    expires_in: int
    user_id: uuid.UUID
    role: str


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    name: str
    reporting_currency: str
    timezone: str
    role: str
    data_version: int
    onboarded_at: dt.datetime | None
    created_at: dt.datetime


class ProfilePatch(BaseModel):
    """Changing reporting_currency is a VIEW change: no data is relabeled or converted."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    reporting_currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = Field(default=None, max_length=64)


class SettingsOut(BaseModel):
    digest_enabled: bool
    digest_channel: Literal["in_app", "email", "telegram"]
    digest_dow: int
    digest_hour: int
    risk_alert_enabled: bool
    cash_buffer: Decimal | None
    telegram_linked: bool

    @field_serializer("cash_buffer")
    def _ser(self, v: Decimal | None) -> str | None:
        return None if v is None else str(v)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest_enabled: bool | None = None
    digest_channel: Literal["in_app", "email", "telegram"] | None = None
    digest_dow: int | None = Field(default=None, ge=0, le=6)
    digest_hour: int | None = Field(default=None, ge=0, le=23)
    risk_alert_enabled: bool | None = None
    cash_buffer: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)


class ConversionOut(BaseModel):
    amount: str
    converted: str
    from_: str = Field(alias="from")
    to: str
    rate: str
    rate_date: dt.date
    source: str
    stale: bool
    approximate: bool
    model_config = ConfigDict(populate_by_name=True)


class FXStatusOut(BaseModel):
    as_of: dt.date | None
    source: str | None
    stale: bool
    approximate: bool
    live: bool


class RateOut(BaseModel):
    currency: str
    rate_inr_per_unit: str
    rate_date: dt.date
    source: str
    stale: bool
    approximate: bool


class CurrencyOut(BaseModel):
    code: str
    name: str
    symbol: str
    locale: str
    minor_units: int


class Message(BaseModel):
    detail: str


# ---------------------------------------------------------------------------- ledger (Phase 2)
class RepeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency: Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"]
    interval: int = Field(default=1, ge=1, le=52)
    end_date: dt.date | None = None


class TxnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expense", "income"]
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    date: dt.date
    description: str = Field(min_length=1, max_length=500)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    category_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)
    fx_rate: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    repeat: RepeatIn | None = None


class TxnPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expense", "income"] | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=2)
    date: dt.date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    category_id: uuid.UUID | None = None
    apply_to_merchant: bool = False
    account_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class TxnFxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fx_rate: Decimal | None = Field(gt=0, max_digits=18, decimal_places=8)


class TxnOut(BaseModel):
    id: uuid.UUID
    date: dt.date
    description: str
    merchant_norm: str | None
    amount: Decimal
    currency: str
    fx_rate: Decimal
    amount_inr: Decimal
    fx_rate_date: dt.date
    fx_source: str
    amount_reporting: Decimal | None
    reporting_currency: str
    category_id: uuid.UUID | None
    category_name: str | None
    category_color: str | None
    cat_source: str | None
    cat_confidence: Decimal | None
    cat_rationale: str | None
    needs_review: bool
    notes: str | None
    source: str
    account_id: uuid.UUID | None
    account_name: str | None
    recurring_rule_id: uuid.UUID | None
    import_batch_id: uuid.UUID | None


class TxnListOut(BaseModel):
    items: list[TxnOut]
    total: int
    limit: int
    offset: int


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    flow_type: Literal["income", "expense", "transfer", "savings"]
    nature: Literal["fixed", "essential_variable", "discretionary", "n/a"] | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=25)


class CategoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=60)
    flow_type: Literal["income", "expense", "transfer", "savings"] | None = None
    nature: Literal["fixed", "essential_variable", "discretionary", "n/a"] | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=500)
    keywords: list[str] | None = Field(default=None, max_length=25)
    archived: bool | None = None
    hidden: bool | None = None


class CategoryOut(BaseModel):
    id: uuid.UUID
    slug: str | None
    name: str
    flow_type: str
    nature: str
    color: str
    icon: str | None
    description: str
    keywords: list[str]
    examples: list[str]
    is_system: bool
    archived: bool
    hidden: bool = False
    txn_count: int = 0


class MergeIn(BaseModel):
    into_id: uuid.UUID


class RecatIn(BaseModel):
    category_id: uuid.UUID
    transaction_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50_000)


class RecatCandidateOut(BaseModel):
    transaction_id: uuid.UUID
    date: str
    description: str
    amount: str
    currency: str
    current_category: str | None
    matched_keyword: str


class SuggestionOut(BaseModel):
    category_id: uuid.UUID
    name: str
    color: str
    icon: str | None
    source: str
    confidence: float


class RecurringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expense", "income"]
    description: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    category_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    frequency: Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"]
    interval: int = Field(default=1, ge=1, le=52)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    weekday: int | None = Field(default=None, ge=0, le=6)
    start_date: dt.date
    end_date: dt.date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class RecurringPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["expense", "income"] | None = None
    description: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    category_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    frequency: Literal["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"] | None = (
        None
    )
    interval: int | None = Field(default=None, ge=1, le=52)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    weekday: int | None = Field(default=None, ge=0, le=6)
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class RecurringOut(BaseModel):
    id: uuid.UUID
    kind: str
    description: str
    amount: Decimal
    currency: str
    category_id: uuid.UUID | None
    category_name: str | None
    account_id: uuid.UUID | None
    frequency: str
    interval: int
    day_of_month: int | None
    weekday: int | None
    start_date: dt.date
    end_date: dt.date | None
    next_run_date: dt.date | None
    status: str
    last_materialized_date: dt.date | None
    skip_dates_json: list[str]
    notes: str | None
    next_occurrences: list[dt.date]


class ImportBatchOut(BaseModel):
    id: uuid.UUID
    filename: str
    file_kind: str
    status: str
    row_counts_json: dict
    created_at: dt.datetime
    imported_at: dt.datetime | None
    undone_at: dt.datetime | None


class ImportMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: dict[str, int | None] | None = None
    options: dict[str, str | bool | None] | None = None


class ImportPreviewOut(BaseModel):
    batch: ImportBatchOut
    headers: list[str]
    mapping: dict
    options: dict
    counts: dict
    rows: list[dict]
    errors: list[dict]
    needs_date_format: bool = False
    date_range: list[dt.date] | None = None


class DashboardOut(BaseModel):
    as_of: dt.date
    period: str
    period_label: str
    window: list[dt.date]
    reporting_currency: str
    data_version: int
    kpis: dict
    trend: list[dict]
    breakdown: list[dict]
    recent: list[TxnOut]
    upcoming: list[dict]
    counts: dict


# ---------------------------------------------------------------------------- analysis (Phase 4)
class DetectedOut(BaseModel):
    id: uuid.UUID
    merchant_norm: str
    description: str
    kind: str
    currency: str
    category_id: uuid.UUID | None
    category_name: str | None
    period: str
    amount_typical: Decimal
    amount_mean: Decimal
    amount_last: Decimal
    amount_cv: Decimal
    n_occurrences: int
    first_seen: dt.date
    last_seen: dt.date
    next_expected: dt.date
    status: str
    confidence: Decimal
    price_history_json: list[dict]
    usage_rating: int | None
    promoted_to_rule_id: uuid.UUID | None
    dismissed: bool
    declared: bool
    suggested: bool


class PromoteOut(BaseModel):
    detected: DetectedOut
    rule: RecurringOut


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: int | None = Field(default=None, ge=0, le=5)
