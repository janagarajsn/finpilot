from __future__ import annotations

from decimal import Decimal

import pytest

from finpilot.money import Money, UnsupportedCurrency, format_money, get_currency_registry, to_money


def test_registry_loads_inr_base_and_usd():
    reg = get_currency_registry()
    assert reg.base == "INR" and set(reg.codes) == {"INR", "USD"}
    assert reg.get("inr").locale == "en_IN"
    assert reg.get("USD").thresholds.small_purchase_ceiling == Decimal(8)
    with pytest.raises(UnsupportedCurrency):
        reg.get("EUR")


@pytest.mark.parametrize(
    "amount, cur, expected",
    [
        ("125000", "INR", "₹1,25,000.00"),
        ("12500000", "INR", "₹1,25,00,000.00"),
        ("125000", "USD", "$125,000.00"),
        ("-1234.5", "INR", "-₹1,234.50"),
        ("0.005", "USD", "$0.00"),  # half-even: 0.005 → 0.00
        ("0.015", "USD", "$0.02"),  # half-even: 0.015 → 0.02
    ],
)
def test_format_money_locale_and_rounding(amount, cur, expected):
    assert format_money(Decimal(amount), cur) == expected


def test_money_arithmetic_is_decimal_and_currency_safe():
    a = Money.of("10.10", "INR") + Money.of("0.20", "INR")
    assert a.amount == Decimal("10.30") and str(a) == "₹10.30"
    assert (Money.of(3, "USD") * 3).amount == Decimal("9.00")
    with pytest.raises(TypeError):
        Money.of(1, "INR") + Money.of(1, "USD")
    with pytest.raises(TypeError):
        Money.of(1, "INR") * 1.5  # type: ignore[operator]


def test_float_boundary_conversion_avoids_binary_artifacts():
    assert to_money(0.1 + 0.2, "INR").amount == Decimal("0.30")
    assert Money.of("1,25,000.456", "INR").amount == Decimal("125000.46")
