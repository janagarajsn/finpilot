"""Ledger pages render with real data; no executable inline scripts (CSP); JSON blobs are valid."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from finpilot.api.routes_fx import fx_service
from finpilot.categories import service as cats
from finpilot.db.session import user_session
from finpilot.recurring import service as rec
from finpilot.transactions import service as txns
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX
from tests.test_web import HX, form_csrf, signup

pytestmark = [needs_pg, needs_redis]


class ScriptAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline_exec, self.json_blobs, self._in_json = [], {}, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and "src" not in a:
            if a.get("type") == "application/json":
                self._in_json = a.get("id")
            else:
                self.inline_exec.append(a)

    def handle_data(self, data):
        if self._in_json:
            self.json_blobs[self._in_json] = json.loads(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_json = None


@pytest.fixture
def web(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    with TestClient(app, follow_redirects=False) as c:
        signup(c)
        c.post(
            "/web/onboarding",
            headers=HX,
            data={
                "csrf_token": form_csrf(c),
                "reporting_currency": "INR",
                "timezone": "Asia/Kolkata",
            },
        )
        uid = c.get("/me").json()["id"]
        with user_session(uid) as s:
            today = dt.date.today()
            food = cats.name_index(s, uid)["food delivery"]
            txns.create(
                s,
                uid,
                txns.TxnInput(
                    "expense",
                    Decimal("450"),
                    today - dt.timedelta(days=2),
                    'Swiggy "late" <night> order',
                    category_id=food,
                ),
                StubFX(),
            )
            txns.create(
                s,
                uid,
                txns.TxnInput(
                    "expense",
                    Decimal("12"),
                    today - dt.timedelta(days=3),
                    "OPENAI *CHATGPT",
                    currency="USD",
                ),
                StubFX(),
            )
            txns.create(
                s,
                uid,
                txns.TxnInput(
                    "income", Decimal("85000"), today - dt.timedelta(days=5), "SALARY ACME"
                ),
                StubFX(),
            )
            cats.create_category(
                s, uid, {"name": 'Pet\'s "Care"', "flow_type": "expense", "keywords": ["vet"]}
            )
            rec.create_rule(
                s,
                uid,
                {
                    "kind": "expense",
                    "description": "Rent",
                    "amount": "25000",
                    "frequency": "monthly",
                    "start_date": today + dt.timedelta(days=3),
                },
            )
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path, expect",
    [
        ("/", ["Spending trend", "Where it went", "Next 14 days", "Rent"]),
        ("/?period=3m", ["Last 3 months"]),
        ("/app/transactions", ["Swiggy", "OPENAI", "$12.00", "rate"]),
        ("/app/transactions?q=swiggy&kind=expense", ["Swiggy"]),
        ("/app/categories", ["Food Delivery", "yours", "Keywords: vet"]),
        ("/app/recurring", ["Rent", ">Next<"]),
        ("/app/import", ["Download the template", "fx_rate"]),
    ],
)
def test_pages_render_safely(web, path, expect):
    r = web.get(path)
    assert r.status_code == 200, r.text[:300]
    for e in expect:
        assert e in r.text, e
    audit = ScriptAudit()
    audit.feed(r.text)
    assert audit.inline_exec == [], "inline executable <script> breaks CSP"
    assert (
        "fp-ctx" in audit.json_blobs and audit.json_blobs["fp-ctx"]["reporting_currency"] == "INR"
    )


def test_dashboard_chart_data_blob(web):
    audit = ScriptAudit()
    audit.feed(web.get("/").text)
    d = audit.json_blobs["fp-dashboard"]
    assert len(d["trend"]) == 12 and any(b["name"] == "Food Delivery" for b in d["breakdown"])


def test_tricky_text_is_escaped_in_attributes(web):
    html = web.get("/app/categories").text
    assert "<night>" not in web.get("/app/transactions").text  # HTML-escaped
    assert "Pet\\u0027s" in html or "Pet&#39;s" in html  # inside single-quoted x-data via tojson


def test_signed_in_pages_are_never_cached_and_sign_out_ends_the_session(web):
    page = web.get("/app/transactions")
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    assert "no-store" in web.get("/me").headers["cache-control"]  # API answers too
    assert "no-store" not in web.get("/static/app.js").headers.get("cache-control", "")
    old = dict(web.cookies)
    out = web.post(
        "/web/logout", data={"csrf_token": web.cookies.get("fp_csrf")}, follow_redirects=False
    )
    assert out.status_code == 303 and out.headers["location"] == "/login"
    # no Clear-Site-Data: Chrome can take seconds to clear a large cache; no-store pages plus
    # the back/forward-cache reload already keep Back from showing a signed-in page
    assert "clear-site-data" not in out.headers
    # the old session cookies no longer open anything
    replay = TestClient(app)
    replay.cookies.update(old)
    r = replay.get("/app/transactions", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_transactions_period_filter(web):
    future = (dt.date.today() + dt.timedelta(days=400)).isoformat()
    page = web.get("/app/transactions?period=7d").text
    assert "Swiggy" in page and 'value="7d" selected' in page
    assert "No transactions match" in web.get("/app/transactions?period=last_year").text
    # a preset wins over the From/To the form also sends; "Any time" ignores them
    assert "Swiggy" in web.get(f"/app/transactions?period=30d&date_from={future}").text
    assert "Swiggy" in web.get(f"/app/transactions?period=&date_from={future}").text
    # custom range, and old links that carry only dates
    for q in (f"period=custom&date_from={future}", f"date_from={future}"):
        page = web.get(f"/app/transactions?{q}").text
        assert "No transactions match" in page and 'value="custom" selected' in page
    assert web.get("/app/transactions?period=bogus").status_code == 200


@pytest.mark.parametrize(
    "period, today, expect",
    [
        ("this_month", "2026-02-10", ("2026-02-01", "2026-02-28")),
        ("this_month", "2026-12-31", ("2026-12-01", "2026-12-31")),
        ("last_month", "2026-01-15", ("2025-12-01", "2025-12-31")),
        ("last_month", "2026-03-31", ("2026-02-01", "2026-02-28")),
        ("7d", "2026-10-16", ("2026-10-10", "2026-10-16")),
        ("90d", "2026-10-16", ("2026-07-19", "2026-10-16")),
        ("this_year", "2026-10-16", ("2026-01-01", "2026-12-31")),
        ("last_year", "2026-10-16", ("2025-01-01", "2025-12-31")),
    ],
)
def test_period_range(period, today, expect):
    got = txns.period_range(period, dt.date.fromisoformat(today))
    assert got == tuple(dt.date.fromisoformat(d) for d in expect)
    assert txns.period_range("custom", dt.date(2026, 1, 1)) is None
