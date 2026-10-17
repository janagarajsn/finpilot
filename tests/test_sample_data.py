"""Committed sample datasets (data/sample/): files match the import template, every row is
categorized, each dataset loads for 6+ months and Insights finds the planted events."""

from __future__ import annotations

import csv
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from finpilot.api.routes_fx import fx_service
from finpilot.categories.taxonomy import load_taxonomy
from finpilot.ingest import sample_data as sd
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer

COLUMNS = ["date", "description", "amount", "type", "category", "account", "notes", "currency"]


@pytest.mark.parametrize("key", ["inr", "usd", "mixed"])
def test_files_are_valid_import_rows(key):
    ds = sd.manifest()["datasets"][key]
    names = {c.name for c in load_taxonomy()}
    with (sd.SAMPLE_DIR / ds["file"]).open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert set(COLUMNS) <= set(rows[0])
    dates = sorted(dt.date.fromisoformat(r["date"]) for r in rows)
    assert (dates[-1] - dates[0]).days >= 6 * 30
    assert {r["currency"] for r in rows} == set(ds["currencies"])
    for r in rows:
        assert r["category"] in names, r
        assert r["type"] in ("income", "expense") and float(r["amount"]) > 0
        # plain descriptions, not bank strings
        assert "/" not in r["description"] and not r["description"].isupper(), r
    for item in ds["recurring"]:
        assert item["category"] in names


@pytest.mark.usefixtures("redis_client")
@needs_pg
@needs_redis
@pytest.mark.parametrize(
    ("key", "currency", "expected"),
    [
        ("inr", "INR", {"duplicate_charge", "first_time_large", "price_hike"}),
        ("usd", "USD", {"duplicate_charge", "first_time_large", "price_hike"}),
        ("mixed", "INR", {"duplicate_charge", "first_time_large", "fx_exposure"}),
    ],
)
def test_load_each_dataset_shows_insights(key, currency, expected):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        t = api_register(c, currency)
        c.cookies.clear()
        c.headers.update(bearer(t["access_token"]))
        assert c.get("/me/sample-data").json()["loaded"] is None
        r = c.post("/me/sample-data", json={"dataset": key})
        assert r.status_code == 201, r.text
        assert r.json()["dataset"] == key and r.json()["recurring_rules"] >= 5
        assert c.get("/me/sample-data").json()["loaded"] == key
        assert c.post("/me/sample-data", json={"dataset": "usd"}).status_code == 409
        assert c.get("/transactions?uncategorized=true&limit=1").json()["total"] == 0
        d = c.get("/dashboard?period=6m").json()
        assert len([m for m in d["trend"] if m["spend"] != "0"]) >= 6
        types = {f["type"] for f in c.get("/insights").json()["findings"]}
        assert expected <= types, types
        # undo removes the transactions AND the recurring items the dataset declared
        assert c.post(f"/import/{r.json()['batch_id']}/undo").status_code == 200
        assert c.get("/transactions?limit=1").json()["total"] == 0
        assert c.get("/recurring").json() == []
        assert c.get("/me/sample-data").json()["loaded"] is None
    finally:
        app.dependency_overrides.clear()


def test_unknown_dataset_is_rejected(redis_client):
    c = TestClient(app)
    t = api_register(c, "INR")
    c.cookies.clear()
    r = c.post("/me/sample-data", json={"dataset": "nope"}, headers=bearer(t["access_token"]))
    assert r.status_code == 422
