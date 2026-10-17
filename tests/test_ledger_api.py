"""Ledger HTTP API end-to-end (bearer client)."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from finpilot.api.routes_fx import fx_service
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer

pytestmark = [needs_pg, needs_redis]


@pytest.fixture
def api(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    c = TestClient(app)
    t = api_register(c, "INR")
    c.cookies.clear()
    c.headers.update(bearer(t["access_token"]))
    yield c
    app.dependency_overrides.clear()


def test_manual_entry_with_repeat_and_dashboard(api):
    cats = {c["name"]: c["id"] for c in api.get("/categories").json()}
    d1, d2, d3 = (str(dt.date.today() - dt.timedelta(days=n)) for n in (20, 19, 21))
    r = api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "649",
            "date": d1,
            "description": "NETFLIX.COM",
            "category_id": cats["Subscriptions"],
            "repeat": {"frequency": "monthly"},
        },
    )
    assert r.status_code == 201 and r.json()["recurring_rule_id"]
    usd = api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "12",
            "currency": "USD",
            "date": d2,
            "description": "OPENAI *CHATGPT",
        },
    ).json()
    assert (
        usd["amount_inr"] == "-997.80"
        and usd["fx_source"] == "mcp"
        and usd["amount_reporting"] == "-997.80"
    )
    api.post(
        "/transactions",
        json={
            "kind": "income",
            "amount": "85000",
            "date": d3,
            "description": "SALARY ACME",
            "category_id": cats["Salary"],
        },
    )
    rules = api.get("/recurring").json()
    assert len(rules) == 1 and len(rules[0]["next_occurrences"]) == 5
    d = api.get("/dashboard?period=12m").json()
    assert d["reporting_currency"] == "INR" and d["counts"]["transactions"] >= 3
    assert any(b["name"] == "Subscriptions" for b in d["breakdown"])
    assert api.get("/dashboard?period=bogus").status_code == 422


def test_list_filters_patch_and_errors(api):
    for i, desc in enumerate(["Hilltop order", "Riverside order", "Corner trip"]):
        api.post(
            "/transactions",
            json={
                "kind": "expense",
                "amount": str(100 + i),
                "date": f"2024-06-0{i + 1}",
                "description": desc,
            },
        )
    r = api.get("/transactions?q=order&sort=amount_desc").json()
    assert r["total"] == 2 and r["items"][0]["description"] == "Riverside order"
    tid = r["items"][0]["id"]
    assert api.patch(f"/transactions/{tid}", json={"amount": "-5"}).status_code == 422
    assert api.patch(f"/transactions/{tid}", json={"currency": "EUR"}).status_code == 422
    assert (
        api.patch(f"/transactions/{tid}/fx", json={"fx_rate": "80"}).status_code == 422
    )  # INR row
    assert api.get("/transactions/00000000-0000-0000-0000-000000000000").status_code == 404
    assert api.delete(f"/transactions/{tid}").status_code == 204
    assert api.get("/transactions?uncategorized=true").json()["total"] >= 2


def test_category_api_flow(api):
    r = api.post(
        "/categories", json={"name": "Pet Care", "flow_type": "expense", "keywords": ["supertails"]}
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "500",
            "date": "2024-06-01",
            "description": "SUPERTAILS dog food",
        },
    )
    assert api.get("/categories/suggest?q=supertails").json()[0]["name"] == "Pet Care"
    sys_id = next(c["id"] for c in api.get("/categories").json() if c["name"] == "Shopping")
    assert api.patch(f"/categories/{sys_id}", json={"name": "x"}).status_code == 403
    assert api.patch(f"/categories/{sys_id}", json={"hidden": True}).json()["hidden"] is True
    assert api.delete(f"/categories/{pid}").status_code == 409  # in use
    assert api.delete(f"/categories/{pid}?reassign_to={sys_id}").status_code == 204


def test_import_api_wizard(api):
    tpl = api.get("/import/template.csv")
    assert tpl.status_code == 200 and tpl.text.lstrip("﻿").startswith("date,description,amount,type")
    assert (
        api.get("/import/template.xlsx")
        .headers["content-type"]
        .startswith("application/vnd.openxml")
    )
    assert api.get("/import/schema").json()["limits"]["max_rows"] == 50_000
    up = api.post("/import/upload", files={"file": ("t.csv", tpl.content, "text/csv")})
    assert up.status_code == 201 and up.json()["counts"]["new"] == 4
    bid = up.json()["batch"]["id"]
    done = api.post(f"/import/{bid}/confirm")
    assert done.status_code == 202 and done.json()["status"] == "imported"  # eager Celery in tests
    assert api.get(f"/transactions?import_batch_id={bid}").json()["total"] == 4
    assert api.post(f"/import/{bid}/confirm").status_code == 409
    assert "4 transactions removed" in api.post(f"/import/{bid}/undo").json()["detail"]
    big = api.post(
        "/import/upload", files={"file": ("big.csv", b"x" * (5 * 1024 * 1024 + 10), "text/csv")}
    )
    assert big.status_code in (413, 422)


def test_export_csv(api):
    api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "5",
            "date": "2024-06-01",
            "description": "=cmd|' /C calc'!A0",
        },
    )
    r = api.get("/transactions/export.csv")
    assert r.status_code == 200 and "'=cmd" in r.text
