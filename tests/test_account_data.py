"""Your data: JSON export (everything you own, no secrets) and account deletion (password,
cascade, anonymous audit, tokens revoked, other users untouched)."""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.api.main import app
from finpilot.api.routes_fx import fx_service
from finpilot.db.session import system_session, user_session
from finpilot.memory.long_term import MemoryIn, PgMemory
from tests.conftest import needs_pg, needs_redis
from tests.helpers import PASSWORD, StubFX, api_register, bearer, csrf

pytestmark = [needs_pg, needs_redis]


def setup_user(c: TestClient) -> tuple[dict, uuid.UUID]:
    t = api_register(c, "INR")
    h = bearer(t["access_token"])
    assert c.post("/me/sample-data", headers=h).status_code == 201
    uid = uuid.UUID(c.get("/me", headers=h).json()["id"])
    with user_session(uid) as s:
        PgMemory().write(s, uid, MemoryIn(kind="fact", topic="city", text="Lives in Pune"))
    return h, uid


def test_export_contains_everything_owned_and_no_secrets(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        h, uid = setup_user(c)
        other_h, _ = setup_user(TestClient(app))
        r = c.get("/me/export", headers=h)
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        data = r.json()
        assert data["format"] == "finpilot-export/1" and data["profile"]["id"] == str(uid)
        assert len(data["transactions"]) > 50 and data["memories"][0]["text"] == "Lives in Pune"
        assert {t["user_id"] for t in data["transactions"]} == {str(uid)}  # never other users' rows
        raw = r.text
        for secret in ("password_hash", "token_hash", '"embedding"', "$argon2"):
            assert secret not in raw
        assert TestClient(app).get("/me/export").status_code == 401  # anonymous
        del other_h
    finally:
        app.dependency_overrides.clear()


def test_delete_account_cascades_and_is_password_checked(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    try:
        c = TestClient(app)
        h, uid = setup_user(c)
        keep_h, keep = setup_user(TestClient(app))
        assert (
            c.request("DELETE", "/me", json={"password": "wrong-password"}, headers=h).status_code
            == 403
        )
        assert c.request("DELETE", "/me", json={"password": PASSWORD}, headers=h).status_code == 204
        with system_session("test") as s:
            assert (
                s.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": uid}).scalar_one()
                == 0
            )
            for table in ("transactions", "memories", "categories", "recommendations", "digests"):
                n = s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": uid}
                ).scalar_one()
                assert n == 0, table
            ev = s.execute(
                text("""SELECT user_id, detail FROM auth_events WHERE kind = 'account_deleted'
                                   ORDER BY id DESC LIMIT 1""")
            ).one()
            assert (
                ev.user_id is None and ev.detail["user"] and str(uid) not in json.dumps(ev.detail)
            )
            assert (
                s.execute(
                    text("SELECT count(*) FROM transactions WHERE user_id = :u"), {"u": keep}
                ).scalar_one()
                > 0
            )
        assert c.get("/me", headers=h).status_code == 401  # access token revoked
        assert c.get("/me", headers=keep_h).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_delete_account_via_web_form(redis_client):
    c = TestClient(app)
    t = api_register(c, "INR")
    uid = uuid.UUID(t["user_id"]) if "user_id" in t else None
    page = c.get("/account")
    assert "Delete my account" in page.text and "Download my data" in page.text
    tok = csrf(c)["X-CSRF-Token"]
    bad = c.post(
        "/web/account/delete", data={"csrf_token": tok, "password": PASSWORD, "confirm": "nope"}
    )
    assert "Type DELETE to confirm" in bad.text
    ok = c.post(
        "/web/account/delete",
        data={"csrf_token": tok, "password": PASSWORD, "confirm": "DELETE"},
        follow_redirects=False,
    )
    assert ok.status_code in (303, 200) and "/login?deleted=1" in (
        ok.headers.get("location") or ok.headers.get("hx-redirect") or ""
    )
    assert "were deleted" in TestClient(app).get("/login?deleted=1").text
    if uid:
        with system_session("test") as s:
            assert (
                s.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": uid}).scalar_one()
                == 0
            )
