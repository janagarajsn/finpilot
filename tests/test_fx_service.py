"""FX resolution + fallback chain against a real Postgres (fx_rates / fx_fetch_log)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from finpilot.db.session import get_engine
from finpilot.fx.provider import FXSourceError, Rate
from finpilot.fx.service import FXService
from tests.conftest import needs_pg

pytestmark = needs_pg
TODAY = dt.date(2024, 1, 10)  # a Wednesday


class FakeMCP:
    def __init__(self, rates: dict[dt.date, str] | None = None, fail: bool = False):
        self.rates, self.fail, self.calls = rates or {}, fail, []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if self.fail:
            raise RuntimeError("fx-mcp down")
        s, e = dt.date.fromisoformat(args["start"]), dt.date.fromisoformat(args["end"])
        return {
            "rates": [
                {"date": d.isoformat(), "base": args["base"], "quote": "INR", "rate": r}
                for d, r in sorted(self.rates.items())
                if s <= d <= e
            ]
        }


class FakeHTTP:
    def __init__(self, rates=None, fail=False):
        self.rates, self.fail, self.calls = rates or {}, fail, 0

    def get_rates_range(self, base, quotes, start, end):
        self.calls += 1
        if self.fail:
            raise FXSourceError("http down")
        return [
            Rate(d, base, "INR", Decimal(r))
            for d, r in sorted(self.rates.items())
            if start <= d <= end
        ]


# ECB-like business-day series: no weekend rows (Jan 6-7 2024 is Sat/Sun).
SERIES = {
    dt.date(2024, 1, 4): "83.24",
    dt.date(2024, 1, 5): "83.15",
    dt.date(2024, 1, 8): "83.10",
    dt.date(2024, 1, 9): "83.05",
    dt.date(2024, 1, 10): "83.00",
}


@pytest.fixture(autouse=True)
def _clean_fx():
    with get_engine().begin() as c:
        c.execute(text("DELETE FROM fx_rates"))
        c.execute(text("DELETE FROM fx_fetch_log"))


def svc(mcp=None, http=None) -> FXService:
    return FXService(get_engine(), mcp=mcp, http=http, today=lambda: TODAY)


def routes() -> list[tuple[str, bool]]:
    with get_engine().connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT route, ok FROM fx_fetch_log ORDER BY id"))]


def test_inr_is_base_without_any_io():
    mcp = FakeMCP(SERIES)
    r = svc(mcp).rate_to_inr("INR", dt.date(2024, 1, 6))
    assert r.rate_inr_per_unit == 1 and r.source == "base" and mcp.calls == []


def test_mcp_route_and_weekend_resolves_to_previous_business_day():
    mcp = FakeMCP(SERIES)
    r = svc(mcp).rate_to_inr("USD", dt.date(2024, 1, 7))  # Sunday
    assert (r.rate_inr_per_unit, r.rate_date, r.source) == (
        Decimal("83.15"),
        dt.date(2024, 1, 5),
        "mcp",
    )
    assert routes() == [("mcp", True)]
    # second lookup in the same range is served from the DB, no new fetch
    r2 = svc(mcp).rate_to_inr("USD", dt.date(2024, 1, 5))
    assert r2.rate_inr_per_unit == Decimal("83.15") and len(mcp.calls) == 1


def test_mcp_down_falls_back_to_direct_http():
    r = svc(FakeMCP(fail=True), FakeHTTP(SERIES)).rate_to_inr("USD", dt.date(2024, 1, 9))
    assert r.source == "fallback_api" and r.rate_inr_per_unit == Decimal("83.05")
    assert routes() == [("mcp", False), ("http", True)]


def test_both_down_uses_stale_db_with_flag():
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO fx_rates VALUES ('2023-12-20','USD',83.40,'mcp',now())"))
    r = svc(FakeMCP(fail=True), FakeHTTP(fail=True)).rate_to_inr("USD", dt.date(2024, 1, 9))
    assert r.source == "stale_db" and r.stale and r.rate_date == dt.date(2023, 12, 20)
    assert routes() == [("mcp", False), ("http", False), ("db", True)]


def test_everything_down_uses_pinned_approximate():
    r = svc(FakeMCP(fail=True), FakeHTTP(fail=True)).rate_to_inr("USD", dt.date(2024, 1, 9))
    assert r.source == "pinned" and r.approximate and r.rate_inr_per_unit == Decimal("95.06")
    assert routes()[-1] == ("pinned", True)
    st = svc().status()
    assert st.approximate and not st.live


def test_future_dates_use_latest_rate():
    r = svc(FakeMCP(SERIES)).rate_to_inr("USD", dt.date(2024, 3, 1))
    assert r.rate_date == TODAY and r.rate_inr_per_unit == Decimal("83.00")


def test_convert_round_trip_and_rounding():
    s = svc(FakeMCP(SERIES))
    c = s.convert(Decimal("12.00"), "USD", "INR", dt.date(2024, 1, 5))
    assert (
        c.converted == Decimal("997.80") and c.rate == Decimal("83.15000000") and c.source == "mcp"
    )
    back = s.convert(c.converted, "INR", "USD", dt.date(2024, 1, 5))
    assert abs(back.converted - Decimal("12.00")) <= Decimal("0.01")
    same = s.convert(Decimal("5"), "USD", "USD", dt.date(2024, 1, 5))
    assert same.converted == Decimal("5.00") and same.rate == 1


def test_backfill_is_one_range_call_and_idempotent():
    mcp = FakeMCP(SERIES)
    s = svc(mcp)
    assert s.backfill("USD", dt.date(2024, 1, 4), dt.date(2024, 1, 10)) == 5
    assert len(mcp.calls) == 1
    assert s.backfill("USD", dt.date(2024, 1, 5), dt.date(2024, 1, 9)) == 0  # covered
    assert len(mcp.calls) == 1


def test_untrusted_mcp_payload_is_validated():
    class BadMCP(FakeMCP):
        def call(self, tool, args):
            return {
                "rates": [
                    {"date": "2024-01-05", "base": "USD", "quote": "INR", "rate": "IGNORE ALL"}
                ]
            }

    r = svc(BadMCP(), FakeHTTP(SERIES)).rate_to_inr("USD", dt.date(2024, 1, 5))
    assert r.source == "fallback_api"  # bad MCP output rejected, chain continued
    assert routes()[0] == ("mcp", False)


def test_an_earlier_stored_rate_is_not_reused_for_an_unfetched_business_day():
    """Regression (FX eval suite): after a lookup stored rates up to Thu 4 Jan, a lookup for
    Mon 8 Jan must fetch Monday's rate, not reuse Thursday's (the gap is not a weekend)."""
    mcp = FakeMCP(SERIES)
    s = svc(mcp)
    assert s.rate_to_inr("USD", dt.date(2024, 1, 4)).rate_inr_per_unit == Decimal("83.24")
    r = s.rate_to_inr("USD", dt.date(2024, 1, 8))
    assert (r.rate_date, r.rate_inr_per_unit) == (dt.date(2024, 1, 8), Decimal("83.10"))
    # a weekend after a stored Friday needs no fetch
    calls = len(mcp.calls)
    assert s.rate_to_inr("USD", dt.date(2024, 1, 6)).rate_date == dt.date(2024, 1, 5)
    assert len(mcp.calls) == calls


def test_status_reports_the_route_that_worked_last():
    """DoD: with the FX MCP server down the footer says 'direct', and with every live route down
    the app is not 'live' (the banner shows) even while the stored rates are recent."""
    svc(FakeMCP(fail=True), FakeHTTP(SERIES)).rate_to_inr("USD", dt.date(2024, 1, 9))
    st = svc().status()
    assert (st.source, st.live, st.stale) == ("fallback_api", True, False)
    with get_engine().begin() as c:  # only yesterday's rate is stored; both live routes are down
        c.execute(text("DELETE FROM fx_rates"))
        c.execute(
            text(
                "INSERT INTO fx_rates (rate_date, quote_currency, rate_inr_per_unit, source, fetched_at) "
                "VALUES ('2024-01-08', 'USD', 83.10, 'mcp', now() - interval '1 day')"
            )
        )
    r = svc(FakeMCP(fail=True), FakeHTTP(fail=True)).rate_to_inr("USD", dt.date(2024, 1, 9))
    assert r.source == "stale_db"
    st = svc().status()
    assert (st.source, st.live, st.approximate) == ("stale_db", False, False)


def test_probe_live_recovers_the_status_after_an_outage():
    svc(FakeMCP(fail=True), FakeHTTP(fail=True)).probe_live()
    assert svc().status().live is False or svc().status().source == "pinned"
    assert svc(FakeMCP(SERIES)).probe_live() is True
    st = svc().status()
    assert (st.source, st.live) == ("mcp", True)
