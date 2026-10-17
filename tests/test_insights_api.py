"""Insights / subscriptions / detected-repeat HTTP API and pages."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from finpilot.api.routes_fx import fx_service
from finpilot.recurring.rules import _add_months
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer
from tests.test_web_ledger import ScriptAudit, web  # noqa: F401 — shared page fixture

pytestmark = [needs_pg, needs_redis]
TODAY = dt.date.today()


@pytest.fixture
def api(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    c = TestClient(app)
    t = api_register(c, "INR")
    c.cookies.clear()
    c.headers.update(bearer(t["access_token"]))
    yield c
    app.dependency_overrides.clear()


def _monthly(
    c: TestClient, desc: str, amount: str, day: int, n: int = 4, category: str | None = None
) -> None:
    for i in range(1, n + 1):
        body = {
            "kind": "expense",
            "amount": amount,
            "description": desc,
            "date": str(_add_months(TODAY, -i, day)),
        }
        if category:
            body["category_id"] = category
        assert c.post("/transactions", json=body).status_code == 201


def test_subscription_audit_and_usage_rating(api):
    subs_cat = next(c["id"] for c in api.get("/categories").json() if c["name"] == "Subscriptions")
    _monthly(api, "NETFLIX.COM", "649", 3, category=subs_cat)
    _monthly(api, "DISNEY HOTSTAR", "299", 4, category=subs_cat)
    rep = api.get("/insights").json()
    assert any(f["type"] == "subscription_overlap" for f in rep["findings"])
    subs = api.get("/subscriptions").json()
    assert subs["totals"]["count"] == 2 and subs["totals"]["monthly"] == "948.00"
    sid = next(s["id"] for s in subs["subscriptions"] if s["merchant"] == "NETFLIX")
    assert api.post(f"/subscriptions/{sid}/usage", json={"rating": 6}).status_code == 422
    assert api.post(f"/subscriptions/{sid}/usage", json={"rating": 1}).status_code == 200
    netflix = next(s for s in api.get("/subscriptions").json()["subscriptions"] if s["id"] == sid)
    assert "low_value_proxy" in netflix["flags"] and netflix["low_value_basis"] == "user_rating"
    assert api.post(f"/subscriptions/{sid}/usage", json={"rating": None}).status_code == 200


def test_detected_repeat_promote_dismiss_restore(api):
    _monthly(api, "ACT FIBERNET", "1179", 18)
    _monthly(api, "CULT FIT", "1599", 2)
    api.get("/insights")
    rows = {r["merchant_norm"]: r for r in api.get("/recurring/detected?all=true").json()}
    act, cult = rows["ACT FIBERNET"], rows["CULT FIT"]
    assert act["period"] == "monthly" and act["n_occurrences"] == 4 and not act["declared"]
    r = api.post(f"/recurring/detected/{act['id']}/promote")
    assert r.status_code == 201 and r.json()["detected"]["declared"]
    rule = r.json()["rule"]
    assert rule["amount"] == "1179.00" and rule["next_run_date"] > str(TODAY)
    assert api.post(f"/recurring/detected/{act['id']}/promote").status_code == 409
    assert any(x["id"] == rule["id"] for x in api.get("/recurring").json())
    assert api.post(f"/recurring/detected/{cult['id']}/dismiss").json()["dismissed"] is True
    assert api.post(f"/recurring/detected/{cult['id']}/restore").json()["dismissed"] is False
    assert api.post(f"/recurring/detected/{cult['id']}/explode").status_code == 422


def test_finding_dismiss_and_unknown(api):
    for days in (4, 3):
        api.post(
            "/transactions",
            json={
                "kind": "expense",
                "amount": "5000",
                "description": "CROMA",
                "date": str(TODAY - dt.timedelta(days=days)),
            },
        )
    fid = next(
        f["id"] for f in api.get("/insights").json()["findings"] if f["type"] == "duplicate_charge"
    )
    assert api.post(f"/insights/findings/{fid}/dismiss").status_code == 200
    assert all(f["id"] != fid for f in api.get("/insights").json()["findings"])
    assert api.post(f"/insights/findings/{fid}/explode").status_code == 404


def test_insight_pages_render(web):  # noqa: F811
    uid = web.get("/me").json()["id"]
    from decimal import Decimal

    from finpilot.db.session import user_session
    from finpilot.transactions import service as txns

    with user_session(uid) as s:
        for i in range(1, 5):
            txns.create(
                s,
                uid,
                txns.TxnInput(
                    "expense", Decimal("1179"), _add_months(TODAY, -i, 18), "ACT FIBERNET"
                ),
                StubFX(),
            )
        for d in (3, 2):
            txns.create(
                s,
                uid,
                txns.TxnInput(
                    "expense", Decimal("5000"), TODAY - dt.timedelta(days=d), "CROMA <b>X</b>"
                ),
                StubFX(),
                hash_suffix=str(d),
            )
    for path, expect in [
        (
            "/app/insights",
            [
                "Anomalies",
                "Possible duplicate charge",
                "Subscriptions",
                "Patterns",
                "See 2 transactions",
            ],
        ),
        ("/app/recurring", ["We noticed these repeat", "ACT FIBERNET", "Make it recurring"]),
        ("/", ["Quick add", "Where it went"]),
    ]:
        r = web.get(path)
        assert r.status_code == 200, r.text[:300]
        for e in expect:
            assert e in r.text, (path, e)
        assert "<b>X</b>" not in r.text  # merchant text is escaped
        audit = ScriptAudit()
        audit.feed(r.text)
        assert audit.inline_exec == []
    ev = web.get("/app/insights").text.split('href="/app/transactions?ids=')[1].split('"')[0]
    r = web.get(f"/app/transactions?ids={ev}")
    assert r.status_code == 200 and r.text.count("CROMA") >= 2
