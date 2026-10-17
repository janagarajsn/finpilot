"""Forecast / simulate / ask (NL + LangGraph interrupt on Redis) / goals over HTTP."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import system_session
from finpilot.llm.client import FakeBackend, LLMClient
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer

pytestmark = [needs_pg, needs_redis]
TODAY = dt.date.today()


def parser_reply(messages):
    """Fake parser: no amount until the user has answered a clarification."""
    user = messages[-1]["content"]
    if "spaceship" in user:
        return {"intent": "unsupported", "reason": "not a what-if"}
    if "User answered" in user:
        return {"intent": "one_time_purchase", "amount": 80000, "label": "Goa trip"}
    if "$1,200" in user:
        return {"intent": "one_time_purchase", "amount": 1200, "currency": "USD", "label": "Trip"}
    return {"intent": "one_time_purchase", "amount": None, "label": "Goa trip"}


@pytest.fixture
def api(redis_client):
    fake = LLMClient(
        settings=Settings(mesh_api_key="x"),
        backend=FakeBackend({"simulator_parser:parse": lambda m: json.dumps(parser_reply(m))}),
    )
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: fake
    c = TestClient(app)
    t = api_register(c, "INR")
    c.cookies.clear()
    c.headers.update(bearer(t["access_token"]))
    assert c.post("/me/sample-data").status_code == 201
    yield c
    app.dependency_overrides.clear()


def test_forecast_shape_cache_and_invalidation(api):
    f = api.get("/forecast").json()
    assert len(f["dates"]) == 90 == len(f["p10"]) == len(f["p50"]) == len(f["p90"])
    assert all(a <= b <= c for a, b, c in zip(f["p10"], f["p50"], f["p90"], strict=True))
    assert f["month_ends"] and f["outcome"]["verdict"] in ("affordable", "risky", "not_affordable")
    assert f["scheduled"] and any(x["source"] == "declared" for x in f["scheduled"])
    assert not f["low_confidence"] and f["streams"]
    assert api.get("/forecast").json()["id"] == f["id"]  # same data_version → stored run reused
    api.post(
        "/transactions",
        json={"kind": "expense", "amount": "500", "description": "x", "date": str(TODAY)},
    )
    assert api.get("/forecast").json()["id"] != f["id"]


def test_simulate_structured_purchase_and_levers(api):
    r = api.post(
        "/simulate",
        json={"scenario": {"type": "one_time_purchase", "amount": "250000", "label": "Car"}},
    )
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["end_delta_mean"] == "-250000.00" and res["end_delta_p50"] == "-250000.00"
    assert res["result"]["verdict"] in ("risky", "not_affordable") and res["forecast_run_id"]
    assert len(res["series"]["scn_p50"]) == 90
    for lv in res["levers"]:  # every lever was re-simulated and verifiably helps
        assert float(lv["improvement"]) > 0
    answer = r.json()["answer"]
    assert "cushion" in answer and "₹" in answer
    bad = api.post(
        "/simulate",
        json={"scenario": {"type": "one_time_purchase", "amount": "10", "date": "2000-01-01"}},
    )
    assert bad.status_code == 422 and "after" in bad.json()["detail"]


def test_simulate_foreign_currency_states_the_rate(api):
    with system_session("test: real rate") as s:
        s.execute(
            text("""INSERT INTO fx_rates (scope, rate_date, quote_currency, rate_inr_per_unit, source)
                          VALUES ('real', :d, 'USD', 84.25, 'mcp') ON CONFLICT DO NOTHING"""),
            {"d": TODAY - dt.timedelta(days=1)},
        )
    r = api.post(
        "/simulate",
        json={"scenario": {"type": "one_time_purchase", "amount": "1200", "currency": "USD"}},
    ).json()
    fx = r["result"]["fx_used"][0]
    assert fx["currency"] == "USD" and fx["source"] == "stored"
    assert r["result"]["applied"][0]["amount"] == pytest.approx(1200 * float(fx["rate"]), abs=0.01)
    assert "converted at" in r["answer"]


def test_ask_clarifies_then_resumes_from_redis_checkpoint(api):
    first = api.post(
        "/simulate/ask", json={"question": "Can I afford a Goa trip next month?"}
    ).json()
    assert first["status"] == "needs_input" and first["question"] == "How much would it cost?"
    tid = first["thread_id"]
    done = api.post(f"/simulate/ask/{tid}/resume", json={"answer": "about 80k"}).json()
    assert done["status"] == "done", done
    assert (
        done["scenario"]["type"] == "one_time_purchase" and done["scenario"]["amount"] == "80000.00"
    )
    assert done["result"]["end_delta_mean"] == "-80000.00" and done["answer"]
    assert done["clarifications"] == [{"q": "How much would it cost?", "a": "about 80k"}]
    # finished threads cannot be resumed; foreign threads are invisible
    assert api.post(f"/simulate/ask/{tid}/resume", json={"answer": "x"}).status_code == 404
    other = TestClient(app)
    t2 = api_register(other, "INR")
    other.cookies.clear()
    pending = api.post("/simulate/ask", json={"question": "Can I afford a Goa trip?"}).json()[
        "thread_id"
    ]
    assert (
        other.post(
            f"/simulate/ask/{pending}/resume",
            json={"answer": "80k"},
            headers=bearer(t2["access_token"]),
        ).status_code
        == 404
    )


def test_ask_unsupported_and_unavailable(api):
    assert (
        api.post("/simulate/ask", json={"question": "build me a spaceship"}).json()["status"]
        == "unsupported"
    )
    app.dependency_overrides[llm_client] = lambda: LLMClient(settings=Settings(mesh_api_key=None))
    assert api.post("/simulate/ask", json={"question": "can I afford it?"}).status_code == 503


def test_goals_crud_and_projection(api):
    cats = {c["name"]: c["id"] for c in api.get("/categories").json()}
    assert (
        api.post(
            "/goals", json={"kind": "spend_cap", "name": "Dining", "target_amount": "3000"}
        ).status_code
        == 422
    )
    sav = api.post(
        "/goals",
        json={
            "kind": "savings_target",
            "name": "Emergency fund",
            "target_amount": "300000",
            "saved_amount": "50000",
            "target_date": str(TODAY + dt.timedelta(days=365)),
        },
    )
    cap = api.post(
        "/goals",
        json={
            "kind": "spend_cap",
            "name": "Dining cap",
            "target_amount": "3000",
            "category_id": cats["Dining Out"],
        },
    )
    assert sav.status_code == 201 and cap.status_code == 201
    f = api.get("/forecast").json()
    proj = {g["name"]: g for g in f["goals"]}
    em = proj["Emergency fund"]
    assert em["remaining"] == "250000.00" and em["status"] in ("on_track", "behind", "not_on_track")
    dining = proj["Dining cap"]
    assert dining["status"] in ("over", "within") and float(dining["projected"]) >= float(
        dining["month_to_date"]
    )
    gid = sav.json()["id"]
    assert (
        api.patch(f"/goals/{gid}", json={"saved_amount": "300000"}).json()["saved_amount"]
        == "300000.00"
    )
    assert (
        next(g for g in api.get("/forecast").json()["goals"] if g["id"] == gid)["status"]
        == "reached"
    )
    assert (
        api.delete(f"/goals/{gid}").status_code == 204
        and api.get(f"/goals/{gid}").status_code == 404
    )


def test_sparse_account_gets_low_confidence_forecast(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        t = api_register(c, "USD")
        h = bearer(t["access_token"])
        for d in (10, 5, 1):
            c.post(
                "/transactions",
                json={
                    "kind": "expense",
                    "amount": "40",
                    "description": "GROCER",
                    "date": str(TODAY - dt.timedelta(days=d)),
                },
                headers=h,
            )
        f = c.get("/forecast", headers=h).json()
        assert f["low_confidence"] is True and f["months_of_data"] < 2 and len(f["p50"]) == 90
    finally:
        app.dependency_overrides.clear()


def test_forecast_pages_render(redis_client):
    from tests.test_web_ledger import ScriptAudit

    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        t = api_register(c, "INR")
        c.cookies.clear()
        h = bearer(t["access_token"])
        assert c.post("/me/sample-data", headers=h).status_code == 201
        with system_session("test: onboard") as s:
            s.execute(
                text("UPDATE users SET onboarded_at = now() WHERE id = :u"),
                {"u": t["user"]["id"] if "user" in t else c.get("/me", headers=h).json()["id"]},
            )
        cats = {x["name"]: x["id"] for x in c.get("/categories", headers=h).json()}
        c.post(
            "/goals",
            json={
                "kind": "spend_cap",
                "name": "Dining cap",
                "target_amount": "3000",
                "category_id": cats["Dining Out"],
            },
            headers=h,
        )
        for path, expect in [
            (
                "/app/forecast",
                [
                    "Forecast &amp; what-if",
                    "Projected balance",
                    "Month by month",
                    "Dining cap",
                    "fp-forecast",
                ],
            ),
            ("/", ["Projected balance on", "Forecast &amp; what-if"]),
        ]:
            r = c.get(path, headers=h)
            assert r.status_code == 200, r.text[:300]
            for e in expect:
                assert e in r.text, (path, e)
            audit = ScriptAudit()
            audit.feed(r.text)
            assert audit.inline_exec == []
    finally:
        app.dependency_overrides.clear()


def test_one_off_investments_are_not_projected_and_count_as_savings(api):
    """A lump sum moved into an FD is the user's own decision, not a monthly drain: it must not be
    resampled into the cash forecast, and money scheduled into investments (the sample's SIP)
    counts as saved, as on the dashboard's savings rate."""
    before = api.get("/forecast").json()
    sav = next(c["id"] for c in api.get("/categories").json() if c["name"] == "Savings/Investments")
    for days in (20, 50):  # two big one-offs inside the lookback window
        body = {
            "kind": "expense",
            "amount": "1000000",
            "description": "Fixed deposit",
            "date": str(TODAY - dt.timedelta(days=days)),
            "category_id": sav,
        }
        assert api.post("/transactions", json=body).status_code == 201
    after = api.get("/forecast").json()
    assert not any(s["label"] == "Savings/Investments" for s in after["streams"])
    assert float(after["unprojected_savings_monthly"]) > 300000
    # the FDs lower today's cash, not the projected monthly savings
    assert (
        abs(float(after["projected_monthly_savings"]) - float(before["projected_monthly_savings"]))
        < 1
    )
    assert float(after["projected_monthly_savings"]) > 0
    assert float(after["to_savings_monthly"]) > 0  # the declared SIP
    assert float(after["start_balance"]) < float(before["start_balance"]) - 1900000
