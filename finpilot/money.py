"""Money value object, currency registry and locale-aware formatting (Babel).

All money math is `Decimal` with round-half-even at boundaries. Floats are only allowed
inside pandas/numpy computations and must be converted back with `to_money()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import yaml
from babel.numbers import format_currency, format_decimal
from pydantic import BaseModel

from finpilot.config import get_settings


class CurrencyThresholds(BaseModel):
    small_purchase_ceiling: Decimal
    min_anomaly_amount: Decimal
    default_cash_buffer: Decimal
    grounding_abs_tolerance: Decimal


class CurrencyInfo(BaseModel):
    code: str
    name: str
    symbol: str
    minor_units: int = 2
    locale: str
    aliases: list[str] = []
    thresholds: CurrencyThresholds

    @property
    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.minor_units)


class CurrencyRegistry:
    def __init__(self, base: str, currencies: dict[str, CurrencyInfo]) -> None:
        if base not in currencies:
            raise ValueError(f"base currency {base} missing from registry")
        self.base = base
        self._c = currencies

    @property
    def codes(self) -> list[str]:
        return list(self._c)

    def get(self, code: str) -> CurrencyInfo:
        try:
            return self._c[code.upper()]
        except KeyError:
            raise UnsupportedCurrency(code) from None

    def is_supported(self, code: str | None) -> bool:
        return bool(code) and code.upper() in self._c  # type: ignore[union-attr]


class UnsupportedCurrency(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(f"unsupported currency: {code!r}")
        self.code = code


@lru_cache
def get_currency_registry() -> CurrencyRegistry:
    raw: dict[str, Any] = yaml.safe_load(
        (get_settings().config_dir / "currencies.yml").read_text(encoding="utf-8")
    )
    currencies = {code: CurrencyInfo(code=code, **info) for code, info in raw["currencies"].items()}
    base = get_settings().base_currency or raw.get("base", "INR")
    return CurrencyRegistry(base, currencies)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)  # avoid binary-float artifacts
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as e:
        raise ValueError(f"not a number: {value!r}") from e


def quantize(amount: Decimal, currency: str) -> Decimal:
    q = get_currency_registry().get(currency).quantum
    return amount.quantize(q, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        reg = get_currency_registry()
        code = self.currency.upper()
        reg.get(code)
        object.__setattr__(self, "currency", code)
        object.__setattr__(self, "amount", quantize(to_decimal(self.amount), code))

    @classmethod
    def of(cls, amount: Any, currency: str) -> Money:
        return cls(to_decimal(amount), currency)

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money) or other.currency != self.currency:
            raise TypeError("money arithmetic requires the same currency (convert first)")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        if isinstance(factor, float):
            raise TypeError("multiply Money by Decimal/int, not float")
        return Money(self.amount * Decimal(factor), self.currency)

    def format(self, locale: str | None = None) -> str:
        return format_money(self.amount, self.currency, locale)

    def __str__(self) -> str:
        return self.format()


def to_money(value: float | Decimal | int, currency: str) -> Money:
    """Boundary conversion from numpy/pandas floats back into Money."""
    return Money(to_decimal(value), currency)


def format_money(
    amount: Decimal | float | int | None, currency: str, locale: str | None = None
) -> str:
    if amount is None:
        return "—"
    info = get_currency_registry().get(currency)
    return format_currency(
        quantize(to_decimal(amount), info.code), info.code, locale=locale or info.locale
    )


def format_number(value: Decimal | float | int, currency: str) -> str:
    """Grouped number without symbol, in the currency's locale (e.g. 1,25,000)."""
    return format_decimal(to_decimal(value), locale=get_currency_registry().get(currency).locale)
