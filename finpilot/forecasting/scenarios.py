"""What-if scenarios (typed) and how each one changes ForecastInputs.

Amounts may be in any supported currency; they are converted to the reporting currency at the
latest STORED rate (as of the forecast date) and the rate used is returned with the result.
Scenario items get their own keys, so (common random numbers) they never disturb the draws of
existing items or of variable spend.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.forecasting.inputs import Context, ForecastError
from finpilot.forecasting.model import ForecastInputs, ScheduledItem
from finpilot.money import get_currency_registry
from finpilot.recurring.rules import _add_months

Money = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]
Currency = Annotated[str | None, Field(default=None, min_length=3, max_length=3)]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OneTimePurchase(_S):
    type: Literal["one_time_purchase"] = "one_time_purchase"
    amount: Money
    currency: Currency = None
    date: dt.date | None = None  # default: tomorrow
    label: str = Field(default="Purchase", max_length=80)


class RecurringCost(_S):
    type: Literal["recurring_cost"] = "recurring_cost"
    amount: Money
    currency: Currency = None
    frequency: Literal["weekly", "monthly", "yearly"] = "monthly"
    start_date: dt.date | None = None
    occurrences: int | None = Field(default=None, ge=1, le=520)
    label: str = Field(default="New recurring cost", max_length=80)


class CancelSubscription(_S):
    type: Literal["cancel_subscription"] = "cancel_subscription"
    subscription_id: str | None = None  # detected_recurrences.id
    merchant: str | None = Field(default=None, max_length=80)
    from_date: dt.date | None = None


class IncomeChange(_S):
    type: Literal["income_change"] = "income_change"
    pct: float | None = Field(default=None, ge=-100, le=500)  # change of scheduled income
    monthly_amount: Decimal | None = Field(
        default=None, max_digits=16, decimal_places=2
    )  # signed delta
    currency: Currency = None
    start_date: dt.date | None = None


class SavingsTransfer(_S):
    type: Literal["savings_transfer"] = "savings_transfer"
    amount: Money
    currency: Currency = None
    frequency: Literal["once", "monthly"] = "monthly"
    start_date: dt.date | None = None


class CategoryCut(_S):
    type: Literal["category_cut"] = "category_cut"
    category: str = Field(min_length=1, max_length=80)  # category id or name
    pct: float = Field(gt=0, le=100)
    start_date: dt.date | None = None


class LoanOrInstallments(_S):
    type: Literal["loan_or_installments"] = "loan_or_installments"
    total: Money
    n: int = Field(ge=1, le=120)
    apr: float = Field(default=0.0, ge=0, le=100)  # percent per year
    currency: Currency = None
    start_date: dt.date | None = None
    label: str = Field(default="Installments", max_length=80)


class Composite(_S):
    type: Literal["composite"] = "composite"
    parts: list[Scenario] = Field(min_length=1, max_length=5)


Scenario = Annotated[
    OneTimePurchase
    | RecurringCost
    | CancelSubscription
    | IncomeChange
    | SavingsTransfer
    | CategoryCut
    | LoanOrInstallments
    | Composite,
    Field(discriminator="type"),
]
Composite.model_rebuild()


class ScenarioAdapter(BaseModel):
    scenario: Scenario


def parse(data: dict[str, Any]) -> Any:
    return ScenarioAdapter.model_validate({"scenario": data}).scenario


def installment_payment(total: Decimal, n: int, apr: float) -> Decimal:
    """Level monthly payment of an amortized loan (apr in percent per year)."""
    r = Decimal(str(apr)) / Decimal(1200)
    pay = total / n if r == 0 else total * r / (1 - (1 + r) ** -n)
    return pay.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class Applier:
    """Applies scenarios to a copy of the baseline inputs; records what was applied."""

    def __init__(self, s: Session, base: ForecastInputs, ctx: Context) -> None:
        self.s, self.base, self.ctx = s, base, ctx
        self.applied: list[dict[str, Any]] = []
        self.fx_used: list[dict[str, Any]] = []
        self._n = 0

    # ------------------------------------------------------------------ helpers
    @property
    def tomorrow(self) -> dt.date:
        return self.base.as_of + dt.timedelta(days=1)

    @property
    def end(self) -> dt.date:
        return self.base.as_of + dt.timedelta(days=self.base.horizon)

    def _date(self, d: dt.date | None, what: str) -> dt.date:
        d = d or self.tomorrow
        if d <= self.base.as_of:
            raise ForecastError(f"The {what} date must be after {self.base.as_of:%d %b %Y}.")
        if d > self.end:
            raise ForecastError(
                f"The {what} date is beyond the {self.base.horizon}-day forecast horizon."
            )
        return d

    def _convert(self, amount: Decimal, currency: str | None) -> float:
        cur = (currency or self.ctx.currency).upper()
        if not get_currency_registry().is_supported(cur):
            raise ForecastError(f"Unsupported currency {cur}.")
        if cur == self.ctx.currency:
            return float(amount)
        rate = self.ctx.rate(self.s, cur)
        if rate.source == "unavailable":
            raise ForecastError(f"No exchange rate available for {cur}.", 503)
        info = rate.as_dict(cur, self.ctx.currency)
        if info not in self.fx_used:
            self.fx_used.append(info)
        return float((amount * rate.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))

    def _item(
        self, d: dt.date, amount: float, label: str, kind: str, currency: str | None
    ) -> ScheduledItem:
        self._n += 1
        cur = (currency or self.ctx.currency).upper()
        return ScheduledItem(
            key=f"scn:{self._n}:{label}:{d.isoformat()}",
            date=d,
            amount=amount,
            label=label,
            source="scenario",
            kind=kind,
            currency=cur if cur != self.ctx.currency else None,
        )

    def _monthly_dates(
        self, start: dt.date, count: int | None, step_months: int = 1
    ) -> list[dt.date]:
        out: list[dt.date] = []
        i = 0
        while True:
            d = _add_months(start, i * step_months, start.day)
            if d > self.end or (count is not None and i >= count):
                return out
            out.append(d)
            i += 1

    # ------------------------------------------------------------------ apply
    def apply(self, sc: Any, inp: ForecastInputs | None = None) -> ForecastInputs:
        inp = inp or replace(
            self.base, scheduled=list(self.base.scheduled), factors=dict(self.base.factors)
        )
        t = sc.type
        if t == "composite":
            for part in sc.parts:
                inp = self.apply(part, inp)
            return inp
        if t == "one_time_purchase":
            d = self._date(sc.date, "purchase")
            amt = self._convert(sc.amount, sc.currency)
            inp.scheduled.append(self._item(d, -amt, sc.label, "expense", sc.currency))
            self.applied.append(
                {"type": t, "date": d.isoformat(), "amount": amt, "label": sc.label}
            )
        elif t == "recurring_cost":
            start = self._date(sc.start_date, "start")
            amt = self._convert(sc.amount, sc.currency)
            if sc.frequency == "weekly":
                dates = [
                    start + dt.timedelta(days=7 * i)
                    for i in range(sc.occurrences or 10_000)
                    if start + dt.timedelta(days=7 * i) <= self.end
                ]
            else:
                dates = self._monthly_dates(
                    start, sc.occurrences, 12 if sc.frequency == "yearly" else 1
                )
            inp.scheduled += [self._item(d, -amt, sc.label, "expense", sc.currency) for d in dates]
            self.applied.append(
                {
                    "type": t,
                    "per_charge": amt,
                    "charges": len(dates),
                    "frequency": sc.frequency,
                    "label": sc.label,
                }
            )
        elif t == "savings_transfer":
            start = self._date(sc.start_date, "start")
            amt = self._convert(sc.amount, sc.currency)
            dates = [start] if sc.frequency == "once" else self._monthly_dates(start, None)
            inp.scheduled += [
                self._item(d, -amt, "Savings transfer", "expense", sc.currency) for d in dates
            ]
            self.applied.append({"type": t, "per_transfer": amt, "transfers": len(dates)})
        elif t == "loan_or_installments":
            start = self._date(sc.start_date, "first payment")
            pay = installment_payment(sc.total, sc.n, sc.apr)
            amt = self._convert(pay, sc.currency)
            dates = self._monthly_dates(start, sc.n)
            inp.scheduled += [self._item(d, -amt, sc.label, "expense", sc.currency) for d in dates]
            self.applied.append(
                {
                    "type": t,
                    "payment": amt,
                    "payment_original": str(pay),
                    "n": sc.n,
                    "apr": sc.apr,
                    "payments_in_horizon": len(dates),
                    "total_cost": str((pay * sc.n).quantize(Decimal("0.01"))),
                }
            )
        elif t == "cancel_subscription":
            start = self._date(sc.from_date, "cancellation") if sc.from_date else self.tomorrow
            det = self._find_subscription(sc)
            refs = {str(det["id"])} | (
                {str(det["promoted_to_rule_id"])} if det["promoted_to_rule_id"] else set()
            )
            removed = [it for it in inp.scheduled if it.ref_id in refs and it.date >= start]
            if not removed:
                raise ForecastError(
                    f"{det['merchant_norm'].title()} has no charge left in the forecast window."
                )
            inp.scheduled = [it for it in inp.scheduled if it not in removed]
            self.applied.append(
                {
                    "type": t,
                    "merchant": det["merchant_norm"],
                    "charges_removed": len(removed),
                    "saved": -sum(it.amount for it in removed),
                }
            )
        elif t == "income_change":
            start = self._date(sc.start_date, "start") if sc.start_date else self.tomorrow
            if sc.pct is not None:
                changed = 0
                new = []
                for it in inp.scheduled:
                    if it.kind == "income" and it.date >= start:
                        it = replace(it, amount=it.amount * (1 + sc.pct / 100))
                        changed += 1
                    new.append(it)
                if not changed:
                    raise ForecastError(
                        "No scheduled income in the forecast window to change. Add your salary as a recurring item first."
                    )
                inp.scheduled = new
                self.applied.append({"type": t, "pct": sc.pct, "incomes_changed": changed})
            elif sc.monthly_amount is not None:
                amt = self._convert(abs(sc.monthly_amount), sc.currency) * (
                    1 if sc.monthly_amount > 0 else -1
                )
                dates = self._monthly_dates(start, None)
                inp.scheduled += [
                    self._item(
                        d, amt, "Income change", "income" if amt > 0 else "expense", sc.currency
                    )
                    for d in dates
                ]
                self.applied.append({"type": t, "monthly_amount": amt, "months": len(dates)})
            else:
                raise ForecastError(
                    "Say by how much income changes (a percentage or a monthly amount)."
                )
        elif t == "category_cut":
            start = self._date(sc.start_date, "start") if sc.start_date else self.tomorrow
            key = self.ctx.streams_by_category.get(sc.category) or self.ctx.streams_by_category.get(
                sc.category.lower()
            )
            if key is None:
                raise ForecastError(f"No recent spending in '{sc.category}' to cut.")
            inp.factors[key] = (start, 1 - sc.pct / 100)
            self.applied.append({"type": t, "category": sc.category, "pct": sc.pct, "stream": key})
        else:  # pragma: no cover - guarded by the discriminated union
            raise ForecastError(f"Unknown scenario type {t}.")
        return inp

    def _find_subscription(self, sc: CancelSubscription) -> dict[str, Any]:
        active = [
            d for d in self.ctx.detected if d["kind"] == "expense" and d["status"] == "active"
        ]
        if sc.subscription_id:
            hit = [d for d in active if str(d["id"]) == sc.subscription_id]
        elif sc.merchant:
            q = sc.merchant.strip().upper()
            hit = [
                d
                for d in active
                if q in d["merchant_norm"].upper() or d["merchant_norm"].upper() in q
            ]
        else:
            raise ForecastError("Which subscription should be cancelled?")
        if not hit:
            raise ForecastError(
                "That recurring charge was not found among your active subscriptions.", 404
            )
        return max(hit, key=lambda d: d["amount_typical"])


def clone(inp: ForecastInputs) -> ForecastInputs:
    return replace(inp, scheduled=list(inp.scheduled), factors=copy.copy(inp.factors))
