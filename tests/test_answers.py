"""Answers + caching: L2 keys and invalidation, isolation, degraded not cached, single-flight,
router layers (rules → embeddings → LLM → default), L3 semantic cache, answer log, prewarm, API."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.agents import intent_router as router
from finpilot.answers import service
from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import user_session
from finpilot.llm.client import FakeBackend, LLMClient
from finpilot.redis_client import get_redis
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer

pytestmark = [needs_pg, needs_redis]
NO_MESH = LLMClient(settings=Settings(mesh_api_key=None))


def classify(messages):
    if "paycheque" in messages[-1]["content"]:
        return json.dumps({"intent": "income_summary", "period": "last_month", "confidence": 0.9})
    return json.dumps({"intent": "free_form", "period": None, "confidence": 0.9})


def fake_llm(calls: list | None = None, answer: str = "You spent less than before.") -> LLMClient:
    def reply(messages):
        if calls is not None:
            calls.append(messages)
        return json.dumps({"answer": answer})

    return LLMClient(
        settings=Settings(mesh_api_key="x", embeddings_enabled=True),
        backend=FakeBackend({"answers:answer": reply, "intent_router:classify": classify}),
    )


def user_with_data(client: TestClient, currency: str = "INR") -> tuple[uuid.UUID, dict]:
    t = api_register(client, currency)
    h = bearer(t["access_token"])
    assert client.post("/me/sample-data", headers=h).status_code == 201
    return uuid.UUID(client.get("/me", headers=h).json()["id"]), h


@pytest.fixture
def api(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: NO_MESH
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def ask(uid, **kw) -> service.AnswerOut:
    with user_session(uid) as s:
        return service.answer(s, uid, fx=StubFX(), **kw)


def test_l2_key_composition():
    k = service.l2_key(
        "u1", "top_categories", "3m", 7, "INR", "en_IN", "t1234", "none", "2026-10-16", None
    )
    assert k == "cache:ans:u1:top_categories:3m:7:INR:en_IN:t1234:none:2026-10-16"
    assert service.l2_key(
        "u1",
        "subscription_review",
        "this_month",
        7,
        "USD",
        "en_US",
        "t1",
        "none",
        None,
        "2026-10-15",
    ).endswith(":USD:en_US:t1:none:fx2026-10-15")


def test_l2_hit_then_invalidated_by_a_write_and_a_currency_change(api):
    uid, h = user_with_data(api)
    get_redis().flushdb()  # forget what the sample-data pipeline pre-warmed
    first = ask(uid, intent="top_categories", llm=NO_MESH)
    second = ask(uid, intent="top_categories", llm=NO_MESH)
    assert (first.cache_layer, second.cache_layer) == (
        "miss",
        "L2",
    ) and first.answer == second.answer
    assert second.latency_ms <= first.latency_ms + 5
    api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "9999",
            "description": "BIG TV",
            "date": str(dt.date.today()),
        },
        headers=h,
    )
    after = ask(uid, intent="top_categories", llm=NO_MESH)
    assert after.cache_layer == "miss" and after.answer != first.answer  # data_version moved on
    api.patch("/me", json={"reporting_currency": "USD"}, headers=h)
    usd = ask(uid, intent="top_categories", llm=NO_MESH)
    assert usd.cache_layer == "miss" and "$" in usd.answer


def test_answers_never_cross_users(api):
    a, _ = user_with_data(api)
    b, hb = user_with_data(api)
    api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "123456",
            "description": "ONLY B",
            "date": str(dt.date.today()),
        },
        headers=hb,
    )
    ra = ask(a, intent="top_categories", llm=NO_MESH)
    rb = ask(b, intent="top_categories", llm=NO_MESH)
    assert rb.cache_layer == "miss" and ra.answer != rb.answer
    with user_session(a) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM answer_log WHERE user_id = :b"), {"b": b}
            ).scalar_one()
            == 0
        )


def test_degraded_answers_are_not_cached_but_llm_answers_are(api):
    uid, _ = user_with_data(api)
    get_redis().flushdb()
    d1, d2 = (
        ask(uid, intent="spending_trend", llm=NO_MESH),
        ask(uid, intent="spending_trend", llm=NO_MESH),
    )
    assert d1.degraded and d2.cache_layer == "miss"  # Mesh down → template, not cached
    calls: list = []
    llm = fake_llm(calls)
    l1, l2 = ask(uid, intent="spending_trend", llm=llm), ask(uid, intent="spending_trend", llm=llm)
    assert (
        l1.generated_by == "llm"
        and l1.grounding["passed"]
        and l2.cache_layer == "L2"
        and len(calls) == 1
    )
    bad = fake_llm(
        answer="You spent ₹9,87,654 this month."
    )  # invented number → guard → template (cached)
    b1 = ask(uid, intent="where_overspending", llm=bad)
    assert b1.generated_by == "template" and not b1.degraded and b1.grounding["fallback_used"]


def test_single_flight_computes_once_for_concurrent_misses(api, monkeypatch):
    uid, _ = user_with_data(api)
    get_redis().flushdb()
    real = service._compute
    calls = []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(0.4)
        return real(*a, **k)

    monkeypatch.setattr(service, "_compute", slow)
    out: list = []
    threads = [
        threading.Thread(target=lambda: out.append(ask(uid, intent="savings_rate", llm=NO_MESH)))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and sorted(o.cache_layer for o in out) == ["L2", "L2", "miss"]
    assert len({o.answer for o in out}) == 1


def test_router_layers():
    r = router.route("Where am I spending the most last month?")
    assert (r.intent, r.period, r.via) == ("where_overspending", "last_month", "rules")
    assert router.route("Can I afford a ₹80,000 trip?").intent == "afford_purchase"
    assert router.route("blah blah", None).via == "default"
    llm = fake_llm()
    emb = router.route("Is it okay to spend 50k on a phone?", llm)  # an example phrasing, no rule
    assert (emb.intent, emb.via) == ("afford_purchase", "embedding") and emb.vector is not None
    cls = router.route("tell me about my paycheques", llm)  # no rule, no close example → fast LLM
    assert (cls.intent, cls.period, cls.via) == ("income_summary", "last_month", "llm")


def test_semantic_cache_reuses_close_phrasing_for_the_same_user_only(api):
    a, ha = user_with_data(api)
    b, _ = user_with_data(api)
    llm = fake_llm()
    q = "Summarise my finances in one line please"
    first = ask(a, question=q, llm=llm)
    again = ask(a, question=q, llm=llm)
    other = ask(b, question=q, llm=llm)
    assert first.intent == "free_form" and first.cache_layer == "miss"
    assert again.cache_layer == "L3" and other.cache_layer == "miss"
    api.post(
        "/transactions",
        json={"kind": "expense", "amount": "50", "description": "X", "date": str(dt.date.today())},
        headers=ha,
    )
    assert ask(a, question=q, llm=llm).cache_layer == "miss"  # new data_version


def test_prewarm_makes_chips_instant_and_api(api):
    uid, h = user_with_data(api)
    get_redis().flushdb()
    with user_session(uid) as s:
        status = service.prewarm(s, uid, fake_llm(), StubFX())
    assert set(status) == set(service.CHIPS)
    r = api.get("/answers?intent=where_to_save", headers=h).json()
    assert r["cache_layer"] == "L2" and r["generated_by"] == "llm"
    assert api.get("/answers?intent=afford_purchase", headers=h).status_code == 404
    afford = api.post("/answers", json={"question": "Can I afford a 50k phone?"}, headers=h).json()
    assert (
        afford["intent"] == "afford_purchase"
        and afford["cache_layer"] == "none"
        and "unavailable" in afford["answer"]
    )
    stats = {x["cache_layer"]: x for x in api.get("/answers/stats", headers=h).json()}
    assert stats["L2"]["n"] >= 1 and stats["L2"]["p95"] < 300


def test_login_prewarms_stale_answers_once(api):
    from tests.helpers import PASSWORD, csrf, unique_email

    email = unique_email("warm")
    t = api_register(api, email=email)
    h = bearer(t["access_token"])
    uid = uuid.UUID(api.get("/me", headers=h).json()["id"])
    with user_session(
        uid
    ) as s:  # e.g. yesterday's snapshot (eager Celery already ran the pipeline)
        s.execute(text("UPDATE insight_snapshots SET as_of = as_of - 1 WHERE kind = 'answers'"))
        assert service.is_stale(s, uid)
    assert (
        api.post(
            "/auth/login", json={"email": email, "password": PASSWORD}, headers=csrf(api)
        ).status_code
        == 200
    )
    with user_session(uid) as s:
        assert not service.is_stale(s, uid)  # eager Celery: the login task pre-warmed the chips
        n = s.execute(text("SELECT count(*) FROM answer_log")).scalar_one()
    assert n == 0  # pre-warming is not a user question
    with user_session(uid) as s:
        s.execute(text("UPDATE users SET reporting_currency = 'USD' WHERE id = :u"), {"u": uid})
        assert service.is_stale(s, uid)  # pre-warmed in another currency
