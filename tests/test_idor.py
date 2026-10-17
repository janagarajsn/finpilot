"""Tenancy at the API layer: identity only ever comes from the token."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from tests.conftest import needs_pg, needs_redis
from tests.helpers import api_register, bearer
from tests.idor import CASES, run_case

pytestmark = [needs_pg, needs_redis]

PUBLIC = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/currencies"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/password/forgot"),
    ("POST", "/auth/password/reset"),
}


@pytest.fixture
def users(redis_client):
    c = TestClient(app)
    a = api_register(c, "INR", name="Alice")
    c.cookies.clear()
    b = api_register(c, "USD", name="Bob")
    return TestClient(app), bearer(a["access_token"]), bearer(b["access_token"]), a, b


def test_every_non_public_route_requires_auth(redis_client):
    api = TestClient(app)
    unprotected = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            m = method.upper()
            if (m, path) in PUBLIC:
                continue
            url = path.replace("{", "").replace("}", "")
            r = api.request(m, url, json={})
            if r.status_code != 401:
                unprotected.append(f"{m} {path} → {r.status_code}")
    assert not unprotected, unprotected


def test_me_and_settings_are_always_the_token_owner(users):
    api, ha, hb, a, b = users
    assert api.get("/me", headers=ha).json()["name"] == "Alice"
    assert api.get("/me", headers=hb).json()["name"] == "Bob"
    # attempts to smuggle another user's id are rejected or ignored
    assert api.patch("/me", json={"id": b["user_id"], "name": "x"}, headers=ha).status_code == 422
    assert api.get(f"/me?user_id={b['user_id']}", headers=ha).json()["id"] == a["user_id"]
    api.patch("/settings", json={"digest_hour": 22}, headers=ha)
    assert api.get("/settings", headers=hb).json()["digest_hour"] == 9


def test_registered_idor_cases(users):
    api, ha, hb, _, _ = users
    for case in CASES:  # populated by later phases
        run_case(case, api, ha, hb)
