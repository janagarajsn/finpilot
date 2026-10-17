from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
import respx

from finpilot.fx.provider import FrankfurterProvider, FXSourceError

BASE = "https://fx.test/v2"


@pytest.fixture
def prov():
    return FrankfurterProvider(base_url="https://fx.test/v1", provider="ECB")  # legacy URL → v2


@respx.mock
def test_get_rate_weekend_returns_previous_business_day(prov):
    route = respx.get(f"{BASE}/rates").mock(
        return_value=httpx.Response(
            200, json=[{"date": "2024-01-05", "base": "USD", "quote": "INR", "rate": 83.15}]
        )
    )
    r = prov.get_rate("usd", "inr", dt.date(2024, 1, 7))
    assert r.rate == Decimal("83.15") and r.date == dt.date(2024, 1, 5)
    q = route.calls.last.request.url.params
    assert (
        q["base"] == "USD"
        and q["quotes"] == "INR"
        and q["date"] == "2024-01-07"
        and q["providers"] == "ECB"
    )


@respx.mock
def test_range_parses_all_rows(prov):
    respx.get(f"{BASE}/rates").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"date": "2024-01-04", "base": "USD", "quote": "INR", "rate": 83.24},
                {"date": "2024-01-05", "base": "USD", "quote": "INR", "rate": 83.15},
            ],
        )
    )
    rows = prov.get_rates_range("USD", ["INR"], dt.date(2024, 1, 4), dt.date(2024, 1, 8))
    assert [r.rate for r in rows] == [Decimal("83.24"), Decimal("83.15")]


def test_same_currency_is_identity_without_network(prov):
    assert prov.get_rate("INR", "INR").rate == Decimal(1)
    assert prov.get_rates_range("INR", ["INR"], dt.date(2024, 1, 1), dt.date(2024, 1, 2)) == []


@respx.mock
@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(422, json={"status": 422, "message": "invalid currency"}),
        httpx.Response(200, json={"weird": True}),
        httpx.Response(200, json=[{"date": "nope"}]),
    ],
)
def test_errors_raise_fx_source_error(prov, resp):
    respx.get(f"{BASE}/rates").mock(return_value=resp)
    with pytest.raises(FXSourceError):
        prov.get_rate("USD", "INR")


@respx.mock
def test_network_failure_raises(prov):
    respx.get(f"{BASE}/rates").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(FXSourceError, match="unreachable"):
        prov.get_rate("USD", "INR")


@pytest.mark.live
def test_live_frankfurter_serves_usd_inr():
    r = FrankfurterProvider().get_rate("USD", "INR", dt.date(2024, 1, 7))
    assert r.date == dt.date(2024, 1, 5) and Decimal(70) < r.rate < Decimal(120)
