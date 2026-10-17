"""Coordinator (LangGraph on the Redis checkpointer): node order + agent_runs + progress, skips,
degraded mode (zero LLM calls), Mesh failure mid-run, crash → resume, intents, API + SSE, goals."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eval import loader
from eval.synth.generator import generate
from eval.synth.personas import get_persona
from finpilot.agents import coordinator as co
from finpilot.agents import goal_tracker
from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import system_session, user_session
from finpilot.llm.client import FakeBackend, LLMClient, RateLimitError
from tests.conftest import needs_pg, needs_redis
from tests.helpers import PASSWORD, StubFX, api_register, bearer, csrf

pytestmark = [needs_pg, needs_redis]
FULL = [
    "ingest",
    "categorize",
    "analyze",
    "forecast",
    "goals",
    "advise",
    "persist_trail",
    "snapshot",
]


@pytest.fixture
def persona(redis_client):
    ds = generate(get_persona("inr_salaried_emi_sip"))
    ld = loader.load(ds, "cotest", with_opening_balance=True)
    yield ld.user_id, max(t.date for t in ds.history())
    loader.cleanup("cotest")


def steps(uid, run_id) -> list[tuple[str, str]]:
    with user_session(uid) as s:
        return [
            (r[0], r[1])
            for r in s.execute(
                text(
                    "SELECT step, status FROM agent_runs WHERE run_id = :r AND user_id = :u ORDER BY id"
                ),
                {"r": run_id, "u": uid},
            )
        ]


def test_degraded_pipeline_runs_every_node_and_records_the_trace(persona):
    uid, as_of = persona
    rid = co.create_run(uid, "manual")
    out = co.run(uid, rid, as_of=as_of, llm=None)
    assert (
        out["status"] == "succeeded"
        and out["degraded"]
        and out["degraded_reason"] == "Mesh API not configured"
    )
    st = steps(uid, rid)
    assert [s for s, _ in st] == [*FULL, "deliver"] and st[-1] == ("deliver", "skipped")
    assert dict(st)["categorize"] == "degraded" and dict(st)["advise"] == "degraded"
    with user_session(uid) as s:
        run = co.get_run(s, uid, rid)
        recs = s.execute(
            text(
                "SELECT count(*), bool_and(generated_by = 'template') FROM recommendations WHERE run_id = :r"
            ),
            {"r": rid},
        ).one()
    assert (
        run["status"] == "succeeded"
        and run["degraded"]
        and run["data_version_end"] >= run["data_version_start"]
    )
    assert recs[0] == 3 and recs[1] is True  # advice linked to this run, templates in degraded mode
    prog = co.read_progress(rid)
    assert prog["status"] == "succeeded" and [x["step"] for x in prog["steps"]] == FULL


def test_nothing_to_categorize_skips_the_node(persona):
    uid, as_of = persona
    co.run(
        uid, co.create_run(uid, "manual"), as_of=as_of, llm=None
    )  # rules categorize what they can
    with user_session(uid) as s:  # pretend the rest was reviewed by the user
        s.execute(
            text(
                "UPDATE transactions SET needs_review = true WHERE user_id = :u AND category_id IS NULL"
            ),
            {"u": uid},
        )
    rid = co.create_run(uid, "manual")
    out = co.run(uid, rid, as_of=as_of, llm=None)
    st = steps(uid, rid)
    assert "categorize" in out["plan"] and ("categorize", "skipped") in st
    assert [s for s, status in st if status != "skipped"] == [x for x in FULL if x != "categorize"]


def test_mesh_failure_mid_run_degrades_and_makes_no_further_llm_calls(persona):
    uid, as_of = persona
    calls: list = []

    def boom(messages):
        calls.append(messages)
        raise RateLimitError(
            "down",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )

    llm = LLMClient(
        settings=Settings(mesh_api_key="x", mesh_max_retries=1), backend=FakeBackend({"*": boom})
    )
    rid = co.create_run(uid, "manual")
    out = co.run(uid, rid, as_of=as_of, llm=llm)
    assert (
        out["status"] == "succeeded"
        and out["degraded"]
        and out["degraded_reason"].startswith("categorizer")
    )
    assert len(calls) == 1  # the categorizer's first batch failed; nothing after it called Mesh
    with user_session(uid) as s:
        gen = (
            s.execute(
                text("SELECT DISTINCT generated_by FROM recommendations WHERE run_id = :r"),
                {"r": rid},
            )
            .scalars()
            .all()
        )
    assert gen == ["template"]


def test_crash_resumes_from_the_failed_node(persona):
    uid, as_of = persona
    rid = co.create_run(uid, "manual")
    out = co.run(uid, rid, as_of=as_of, llm=None, fail_at="forecast")
    assert out["status"] == "failed" and "injected failure in forecast" in out["error"]
    with pytest.raises(co.PipelineError):
        co.resume_run(uid, co.create_run(uid, "manual"))  # no checkpoint for a never-started run
    done = co.resume_run(uid, rid)
    assert done["status"] == "succeeded"
    st = steps(uid, rid)
    names = [s for s, _ in st]
    assert (
        names.count("ingest") == 1 and names.count("analyze") == 1
    )  # finished nodes are not re-run
    assert ("forecast", "failed") in st and ("forecast", "succeeded") in st
    with pytest.raises(co.PipelineError, match="already finished"):
        co.resume_run(uid, rid)


def test_goal_tracker_snapshots_weekly_and_flags_goals_ahead(persona):
    uid, as_of = persona
    with user_session(uid) as s:
        s.execute(
            text("""INSERT INTO goals (user_id, kind, name, target_amount, currency, saved_amount, target_date)
                          VALUES (:u, 'savings_target', 'Trip', 50000, 'INR', 10000, :td)"""),
            {"u": uid, "td": as_of + dt.timedelta(days=700)},
        )
        loan = s.execute(text("SELECT id FROM categories WHERE slug = 'loan_emi'")).scalar_one()
        s.execute(
            text("""INSERT INTO goals (user_id, kind, name, target_amount, currency, saved_amount, category_id)
                          VALUES (:u, 'debt_payoff', 'Bike loan', 100000, 'INR', 25000, :c)"""),
            {"u": uid, "c": loan},
        )
    for _ in range(2):  # same ISO week → one snapshot per goal
        rid = co.create_run(uid, "manual")
        out = co.run(uid, rid, as_of=as_of, llm=None)
    assert out["results"]["goals"]["goals"] == 2
    with user_session(uid) as s:
        n = s.execute(
            text("SELECT count(*) FROM goal_snapshots WHERE user_id = :u"), {"u": uid}
        ).scalar_one()
        gid = s.execute(
            text("SELECT id FROM goals WHERE user_id = :u AND kind = 'debt_payoff'"), {"u": uid}
        ).scalar_one()
        hist = goal_tracker.history(s, uid, gid)
        ahead = s.execute(
            text(
                "SELECT count(*) FROM analysis_findings WHERE user_id = :u AND type = 'goal_ahead' AND active"
            ),
            {"u": uid},
        ).scalar_one()
        debt = json.loads(json.dumps(hist[-1], default=str))
    assert n == 2 and hist[-1]["status"] == "on_track" and debt["progress_amount"] == "25000.00"
    assert (
        ahead == 1
    )  # 40k left over ~23 months at the projected savings rate → ahead of the target
    assert out["results"]["goals"]["ahead"] == ["Trip"]


def test_week_key():
    assert (
        goal_tracker.week_key(dt.date(2026, 1, 1)) == "2026-W01"
        and goal_tracker.week_key(dt.date(2025, 12, 31)) == "2026-W01"
    )


def test_pipeline_api_sse_explain_and_tenancy(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: LLMClient(settings=Settings(mesh_api_key=None))
    try:
        c = TestClient(app)
        t = api_register(c, "INR")
        c.cookies.clear()
        h = bearer(t["access_token"])
        assert (
            c.post("/me/sample-data", headers=h).status_code == 201
        )  # eager Celery: a pipeline ran
        auto = c.get("/pipeline/runs", headers=h).json()
        assert auto and auto[0]["status"] == "succeeded" and auto[0]["degraded"] is True
        r = c.post("/pipeline/run", json={}, headers=h)
        assert r.status_code == 202
        rid = r.json()["run_id"]
        detail = c.get(f"/pipeline/runs/{rid}", headers=h).json()
        assert detail["status"] == "succeeded" and [x["step"] for x in detail["steps"]][:3] == [
            "ingest",
            "categorize",
            "analyze",
        ]
        with c.stream("GET", f"/pipeline/runs/{rid}/events", headers=h) as resp:
            body = "".join(resp.iter_text())
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert (
            "event: progress" in body and "event: done" in body and '"status": "succeeded"' in body
        )
        rec = c.get("/recommendations", headers=h).json()["recommendations"][0]
        ex = c.post(
            "/pipeline/run", json={"intent": "explain", "recommendation_id": rec["id"]}, headers=h
        ).json()
        assert ex["answer"]["id"] == rec["id"] and ex["answer"]["findings"]
        assert (
            c.post("/pipeline/run", json={"intent": "ask_affordability"}, headers=h).status_code
            == 422
        )
        # route_intent → answer_from_cache / free_chat (same graph, answered directly)
        cached = c.post(
            "/pipeline/run",
            json={"intent": "answer_from_cache", "answer_intent": "top_categories"},
            headers=h,
        ).json()
        assert cached["answer"]["intent"] == "top_categories" and cached["answer"]["answer"]
        chat = c.post(
            "/pipeline/run",
            json={"intent": "free_chat", "question": "What are my top categories?"},
            headers=h,
        ).json()
        assert chat["answer"]["route"] == "cache" and chat["answer"]["session_id"]
        assert c.get("/chat/sessions", headers=h).json()[0]["id"] == chat["answer"]["session_id"]
        for bad in ({"intent": "free_chat"}, {"intent": "answer_from_cache", "answer_intent": "x"}):
            assert c.post("/pipeline/run", json=bad, headers=h).status_code == 422
        assert c.post(f"/pipeline/runs/{rid}/resume", headers=h).status_code == 409
        # operator screens (Pipeline, Quality) are admin-only, and hidden from others' sidebar
        home = c.get("/", headers=h).text
        if 'href="/app/pipeline"' not in home:
            assert c.get("/app/pipeline", headers=h).status_code == 404
            assert c.get("/app/eval", headers=h).status_code == 404
            me = c.get("/me", headers=h).json()
            with system_session("test") as s:
                s.execute(text("UPDATE users SET role = 'admin' WHERE id = :u"), {"u": me["id"]})
            login = c.post(
                "/auth/login", json={"email": me["email"], "password": PASSWORD}, headers=csrf(c)
            )
            c.cookies.clear()
            h = bearer(login.json()["access_token"])
            assert 'href="/app/pipeline"' in c.get("/", headers=h).text
        page = c.get("/app/pipeline", headers=h)
        assert page.status_code == 200 and "Pipeline" in page.text and rid in page.text
        other = TestClient(app)
        t2 = api_register(other, "INR")
        h2 = bearer(t2["access_token"])
        for method, path in (
            ("GET", f"/pipeline/runs/{rid}"),
            ("GET", f"/pipeline/runs/{rid}/events"),
            ("POST", f"/pipeline/runs/{rid}/resume"),
        ):
            assert other.request(method, path, headers=h2).status_code == 404
        assert other.get("/pipeline/runs", headers=h2).json() == []
    finally:
        app.dependency_overrides.clear()
