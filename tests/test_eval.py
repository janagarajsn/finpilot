"""Eval runner end-to-end on a real DB (offline + full with a fake LLM), eval API and page."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from eval import runner
from eval.suites import categorization
from eval.suites.base import SuiteContext
from finpilot.api.main import app
from finpilot.config import Settings, get_settings
from finpilot.llm.client import FakeBackend, LLMClient
from tests.conftest import needs_pg, needs_redis
from tests.helpers import api_register, bearer

pytestmark = [needs_pg, needs_redis]
ONE = ["inr_with_usd_subscriptions"]  # smallest persona keeps the test fast


def synthetic_users() -> int:
    with create_engine(get_settings().database_admin_url).connect() as c:
        return c.execute(text("SELECT count(*) FROM users WHERE is_synthetic")).scalar_one()


def test_offline_run_is_stored_gated_and_cleaned_up(redis_client):
    run_id, code = runner.run("offline", "ci", personas=ONE, gate=True, langfuse=False)
    assert code == 0 and synthetic_users() == 0
    with create_engine(get_settings().database_admin_url).connect() as c:
        status, summary = c.execute(
            text("SELECT status, summary_json FROM eval_runs WHERE id = :r"), {"r": run_id}
        ).one()
        n_cases = c.execute(
            text("SELECT count(*) FROM eval_cases WHERE run_id = :r"), {"r": run_id}
        ).scalar_one()
    assert status == "passed" and n_cases > 100
    h = summary["headline"]
    assert h["categorization.precision_resolved"] >= 0.9 and 0 < h["categorization.coverage"] < 1
    assert {g["metric"] for g in summary["gates"]} >= {
        "categorization.precision_resolved",
        "categorization.coverage",
        "anomalies.recall",
        "recurring.materializer_idempotent",
    }
    assert (
        h["recurring.materializer_idempotent"] == 1.0 and h["recurring.materializer_correct"] == 1.0
    )
    assert h["recurring.recall"] >= 0.9 and h["anomalies.fp_per_month_clean"] <= 0.35


def test_full_variant_with_fake_llm_reports_layers_and_cost(redis_client):
    def reply(messages):
        ids = re.findall(r"^- (m\d+) ·", messages[-1]["content"], re.M)
        return json.dumps(
            {
                "results": [
                    {"id": i, "category": "Other", "confidence": 0.9, "rationale": "x"} for i in ids
                ]
            }
        )

    llm = LLMClient(
        settings=Settings(mesh_api_key="x"), backend=FakeBackend({"categorizer:classify": reply})
    )
    res = categorization.run(SuiteContext(mode="full", run_tag="rtest", personas=ONE, llm=llm))
    variants = res.details["variants"]
    assert set(variants) == {"rules", "rules_memory", "full"}
    assert (
        variants["full"]["coverage"]
        > variants["rules_memory"]["coverage"]
        >= variants["rules"]["coverage"]
    )
    names = {m.metric for m in res.metrics}
    assert {"llm_calls_per_1k", "layer_share", "unseen_merchant_accuracy"} <= names
    assert res.cases and all("persona" in c.input for c in res.cases)
    assert synthetic_users() == 0


def test_gate_failure_sets_exit_code(redis_client, monkeypatch):
    monkeypatch.setattr(
        runner,
        "thresholds",
        lambda: {"eval_gates": {"offline": {"categorization.coverage": {"min": 0.999}}}},
    )
    _, code = runner.run("offline", "ci", personas=ONE, gate=True, langfuse=False)
    assert code == 1


def test_eval_api_and_page(redis_client):
    runner.run("offline", "manual", personas=ONE, langfuse=False)
    c = TestClient(app)
    t = api_register(c)
    c.cookies.clear()
    h = bearer(t["access_token"])
    runs = c.get("/eval/runs", headers=h).json()
    assert runs and runs[0]["mode"] == "offline"
    detail = c.get(f"/eval/runs/{runs[0]['id']}", headers=h).json()
    assert detail["metrics"] and "insights" in detail
    cases = c.get(f"/eval/runs/{runs[0]['id']}/cases?passed=false&limit=5", headers=h).json()
    assert cases["total"] >= len(cases["items"])
    cmp = c.get(f"/eval/compare?a={runs[-1]['id']}&b={runs[0]['id']}", headers=h).json()
    assert cmp["metrics"]
    if t["role"] != "admin":
        assert c.post("/eval/run", headers=h).status_code == 403


def test_sample_data_load_and_undo(redis_client):
    from finpilot.api.routes_fx import fx_service
    from tests.helpers import StubFX

    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        t = api_register(c, "INR")
        c.cookies.clear()
        h = bearer(t["access_token"])
        r = c.post("/me/sample-data", headers=h)
        assert (
            r.status_code == 201 and r.json()["imported"] > 100 and r.json()["recurring_rules"] >= 3
        )
        assert c.post("/me/sample-data", headers=h).status_code == 409
        txns = c.get("/transactions?limit=1", headers=h).json()
        assert txns["total"] == r.json()["imported"]
        categorized = c.get("/transactions?uncategorized=true&limit=1", headers=h).json()["total"]
        assert categorized < txns["total"] * 0.5  # rules + memory resolve most rows instantly
        d = c.get("/dashboard?period=3m", headers=h).json()
        assert d["kpis"]["income"] != "0" and d["upcoming"]
        assert c.post(f"/import/{r.json()['batch_id']}/undo", headers=h).status_code == 200
        assert c.get("/transactions?limit=1", headers=h).json()["total"] == 0
    finally:
        app.dependency_overrides.clear()
