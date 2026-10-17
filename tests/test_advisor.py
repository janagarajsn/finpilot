"""Advisor: candidates, validator + grounding (retry → fallback), dismiss memory, trail, API."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eval import loader
from eval.synth.generator import generate
from eval.synth.personas import get_persona
from finpilot.agents.advisor import (
    Advisor,
    decide,
    get_trail,
    list_recommendations,
    mentions_entity,
)
from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import user_session
from finpilot.llm.client import FakeBackend, LLMClient
from finpilot.trail.grounding import check
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, api_register, bearer

pytestmark = [needs_pg, needs_redis]


@pytest.fixture(scope="module")
def persona():
    ds = generate(get_persona("inr_overspender_subcreep"))
    ld = loader.load(ds, "advtest")
    loader.apply_truth_categories(ds, ld)
    yield ld.user_id, max(t.date for t in ds.history())
    loader.cleanup("advtest")


def candidates_from(messages) -> list[dict]:
    body = (
        messages[-1]["content"]
        if "CANDIDATES" in messages[-1]["content"]
        else next(m["content"] for m in messages if "CANDIDATES" in m["content"])
    )
    blob = body.split("CANDIDATES (with FACTS):\n", 1)[1].split("\n\nFINDINGS:", 1)[0]
    return json.loads(blob)


def make_llm(calls: list, invent_first: bool = True) -> LLMClient:
    def reply(messages):
        calls.append(messages)
        cands = candidates_from(messages)[:3]
        retry = any(m["role"] == "assistant" for m in messages)
        items = []
        for c in cands:
            name = next(
                v for k, v in c["names"].items() if k not in ("date", "dates", "month") and v
            )
            items.append(
                {
                    "recommendation_id": c["id"],
                    "title": f"Act on {name}",
                    "action": f"Do this for {name}.",
                    "why": "It saves ₹1,23,456 every month."
                    if invent_first and not retry
                    else "Because of the finding.",
                    "cited_finding_ids": c["finding_ids"][:1],
                    "numbers_used": [],
                }
            )
        return json.dumps({"recommendations": items})

    return LLMClient(
        settings=Settings(mesh_api_key="x"), backend=FakeBackend({"advisor:recommend": reply})
    )


def test_template_advice_is_specific_cited_grounded_and_trailed(persona):
    uid, as_of = persona
    with user_session(uid) as s:
        adv = Advisor(None)
        _, _, cands = adv.prepare(s, uid, as_of=as_of)
        assert len(cands) >= 3 and all(c.finding_ids for c in cands)
        kinds = {c.kind for c in cands}
        assert {"cancel_subscription", "category_cap", "close_shortfall"} <= kinds
        sub = next(c for c in cands if c.kind == "cancel_subscription")
        assert (
            sub.simulation["end_change"] > 0 and sub.est_monthly_impact > 0
        )  # cancelling raises the balance
        run = adv.run(s, uid, as_of=as_of)
    assert run.generated_by == "template" and len(run.recommendations) == 3
    by_key = {c.key: c for c in cands}
    for r in run.recommendations:
        c = by_key[r["candidate_key"]]
        assert mentions_entity(f"{r['title']} {r['action']} {r['why']}", c)
        assert set(r["cited_finding_ids"]) <= set(c.finding_ids) and r["cited_finding_ids"]
    with user_session(uid) as s:
        t = get_trail(s, uid, run.recommendations[0]["id"])
    assert t["findings"] and t["forecast_ref_json"]["forecast_run_id"]
    assert (
        t["grounding_report_json"]["this_recommendation"]["passed"] is True
    )  # template numbers are facts
    assert t["prompt_name"] == "templates/messages/advice.yml"


def test_llm_advice_retries_invented_numbers_then_passes(persona):
    from finpilot.memory.long_term import MemoryIn, PgMemory

    uid, as_of = persona
    calls: list = []
    with user_session(uid) as s:  # a pinned preference reaches the prompt as <memory> data
        PgMemory().write(
            s,
            uid,
            MemoryIn(kind="preference", topic="tone", text="Wants advice in one line", pinned=True),
        )
        run = Advisor(make_llm(calls)).run(s, uid, as_of=as_of)
    assert (
        "<memory>" in calls[0][-1]["content"]
        and "Wants advice in one line" in calls[0][-1]["content"]
    )
    assert (
        run.generated_by == "llm"
        and run.guard["attempts"] == 2
        and not run.guard["first_attempt_pass"]
    )
    assert "₹1,23,456" in calls[1][-1]["content"]  # the retry names the invented number
    assert all(r["why"] == "Because of the finding." for r in run.recommendations)
    with user_session(uid) as s:
        t = get_trail(s, uid, run.recommendations[0]["id"])
    assert (
        t["prompt_name"] == "advisor/recommend"
        and t["prompt_version"] == 2
        and t["grounding_report_json"]["retries"] == 1
    )


def test_validator_rejects_uncited_or_generic_advice(persona):
    uid, as_of = persona

    def reply(messages):
        cands = candidates_from(messages)[:3]
        return json.dumps(
            {
                "recommendations": [
                    {
                        "recommendation_id": c["id"],
                        "title": "Save more money",
                        "action": "Make a budget.",
                        "why": "Good habit.",
                        "cited_finding_ids": [],
                        "numbers_used": [],
                    }
                    for c in cands
                ]
            }
        )

    llm = LLMClient(
        settings=Settings(mesh_api_key="x"), backend=FakeBackend({"advisor:recommend": reply})
    )
    with user_session(uid) as s:
        run = Advisor(llm).run(s, uid, as_of=as_of)
    assert (
        run.generated_by == "template" and run.guard["fallback_used"] and run.guard["attempts"] == 3
    )
    probs = " ".join(run.guard["problems"][0])
    assert "cite at least one" in probs and "specifically" in probs


def test_dismissed_advice_is_not_repeated_and_reason_reaches_the_prompt(persona):
    uid, as_of = persona
    with user_session(uid) as s:
        first = Advisor(None).run(s, uid, as_of=as_of)
        top = first.recommendations[0]
        decide(s, uid, top["id"], "dismiss", "We share this with family; don't suggest it")
    calls: list = []
    with user_session(uid) as s:
        second = Advisor(make_llm(calls, invent_first=False)).run(s, uid, as_of=as_of)
        assert top["candidate_key"] not in {r["candidate_key"] for r in second.recommendations}
        assert "don't suggest it" in calls[0][-1]["content"]
        listed = list_recommendations(s, uid)
    assert len(listed["recommendations"]) == 3 and not listed["stale"]


def test_recommendation_api_trail_feedback_and_tenancy(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: LLMClient(settings=Settings(mesh_api_key=None))
    try:
        c = TestClient(app)
        t = api_register(c, "INR")
        c.cookies.clear()
        h = bearer(t["access_token"])
        assert c.get("/recommendations", headers=h).json()["empty"] is True
        assert c.post("/me/sample-data", headers=h).status_code == 201
        r = c.post("/recommendations/refresh", headers=h).json()
        assert r["generated_by"] == "template" and 1 <= len(r["recommendations"]) <= 3
        rid = r["recommendations"][0]["id"]
        tr = c.get(f"/recommendations/{rid}/trail", headers=h).json()
        assert tr["findings"] and tr["trail_trigger"] in (
            "anomaly",
            "subscription",
            "forecast_shortfall",
            "goal_gap",
            "pattern",
        )
        assert (
            c.post(f"/recommendations/{rid}/feedback", json={"value": "up"}, headers=h).json()[
                "feedback"
            ]
            == "up"
        )
        assert c.post(f"/recommendations/{rid}/accept", headers=h).json()["status"] == "accepted"
        assert (
            c.post(
                f"/recommendations/{rid}/dismiss", json={"reason": "changed my mind"}, headers=h
            ).json()["status"]
            == "dismissed"
        )
        assert c.post(f"/recommendations/{rid}/explode", headers=h).status_code == 422
        # pages
        page = c.get("/app/advice", headers=h)
        assert (
            page.status_code == 200
            and r["recommendations"][1]["title"] in page.text
            and "Why?" in page.text
        )
        summ = c.post("/insights/summary", headers=h).json()
        assert summ["generated_by"] == "template" and summ["answer"]
        assert c.post("/insights/summary", headers=h).json()["cached"] is True
    finally:
        app.dependency_overrides.clear()


def test_simulation_answer_is_narrated_under_the_guard(redis_client):
    def verbalize(messages):
        body = messages[-1]["content"]
        facts = json.loads(body.split("FACTS:\n", 1)[1].rsplit("\n</data>", 1)[0])
        low = facts["with_change"]["min_p10"]
        return json.dumps({"answer": f"In a bad case your balance falls to ₹{float(low):,.0f}."})

    fake = LLMClient(
        settings=Settings(mesh_api_key="x"),
        backend=FakeBackend({"simulator_parser:verbalize": verbalize}),
    )
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: fake
    try:
        c = TestClient(app)
        t = api_register(c, "INR")
        c.cookies.clear()
        h = bearer(t["access_token"])
        c.post("/me/sample-data", headers=h)
        r = c.post(
            "/simulate",
            json={"scenario": {"type": "one_time_purchase", "amount": "50000"}},
            headers=h,
        ).json()
        assert r["narration"]["generated_by"] == "llm" and r["narration"]["passed"] is True
        assert re.search(r"₹-?[\d,]+", r["answer"])
    finally:
        app.dependency_overrides.clear()


def test_guard_report_for_unrelated_text_fails():
    from finpilot.trail.grounding import FactSet

    assert not check(["Saves ₹5,000"], FactSet().add_money("649", "INR")).passed


def test_dismissing_a_superseded_card_still_counts(persona):
    uid, as_of = persona
    with user_session(uid) as s:
        old = Advisor(None).run(s, uid, as_of=as_of).recommendations[0]
        new = (
            Advisor(None).run(s, uid, as_of=as_of).recommendations
        )  # background refresh supersedes `old`
        assert decide(s, uid, old["id"], "dismiss", "no")["status"] == "dismissed"
        same = [r for r in new if r["candidate_key"] == old["candidate_key"]]
        status = (
            s.execute(
                text("SELECT status FROM recommendations WHERE id = :i"), {"i": same[0]["id"]}
            ).scalar_one()
            if same
            else "dismissed"
        )
    assert status == "dismissed"
