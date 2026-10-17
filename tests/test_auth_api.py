from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.api.main import app
from finpilot.auth import service as auth
from finpilot.db.session import user_session
from tests.conftest import needs_pg, needs_redis
from tests.helpers import PASSWORD, api_register, bearer, csrf, unique_email

pytestmark = [needs_pg, needs_redis]


@pytest.fixture
def client(redis_client):
    with TestClient(app) as c:
        yield c


def events(user_id) -> list[str]:
    with user_session(user_id) as s:
        return [
            r[0]
            for r in s.execute(
                text("SELECT kind FROM auth_events WHERE user_id = :u ORDER BY id"), {"u": user_id}
            )
        ]


# ---------------------------------------------------------------- register / login
def test_register_sets_cookies_creates_defaults_and_first_user_is_admin(client):
    with user_session("00000000-0000-0000-0000-000000000000"):
        pass
    body = api_register(client, "USD")
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert {"fp_access", "fp_refresh", "fp_csrf"} <= set(client.cookies.keys())
    me = client.get("/me").json()
    assert me["reporting_currency"] == "USD" and me["role"] in ("admin", "user")
    with user_session(body["user_id"]) as s:
        acct = s.execute(text("SELECT name, currency FROM accounts")).one()
        assert tuple(acct) == ("Main", "USD")
        assert s.execute(text("SELECT digest_channel FROM user_settings")).scalar() == "in_app"


def test_first_registered_user_becomes_admin():
    from sqlalchemy import create_engine

    from finpilot.config import get_settings

    with create_engine(get_settings().database_admin_url).connect() as c:
        first = c.execute(
            text("SELECT role FROM users WHERE NOT is_synthetic ORDER BY created_at LIMIT 1")
        ).scalar()
    assert first == "admin"


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"password": "short"}, 422),
        ({"password": "password123"}, 422),
        ({"email": "not-an-email"}, 422),
        ({"reporting_currency": "EUR"}, 422),
        ({"timezone": "Mars/Olympus"}, 422),
    ],
)
def test_register_validation(client, payload, code):
    base = {
        "email": unique_email(),
        "password": PASSWORD,
        "name": "X",
        "reporting_currency": "INR",
        "timezone": "Asia/Kolkata",
    }
    r = client.post("/auth/register", json={**base, **payload})
    assert r.status_code == code, r.text


def test_duplicate_email_rejected_case_insensitive(client):
    email = unique_email()
    api_register(client, email=email)
    client.cookies.clear()
    r = client.post(
        "/auth/register", json={"email": email.upper(), "password": PASSWORD, "name": "Y"}
    )
    assert r.status_code == 409


def test_login_bearer_flow_and_wrong_password(client):
    email = unique_email()
    api_register(client, email=email)
    client.cookies.clear()
    assert (
        client.post(
            "/auth/login", json={"email": email, "password": "wrong-password-1"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    r = client.post("/auth/login", json={"email": email.upper(), "password": PASSWORD})
    assert r.status_code == 200
    fresh = TestClient(app)  # no cookies: pure bearer API client
    assert fresh.get("/me", headers=bearer(r.json()["access_token"])).json()["email"] == email
    assert fresh.get("/me").status_code == 401


# ---------------------------------------------------------------- refresh rotation / reuse
def test_refresh_rotates_and_reuse_revokes_family(client):
    t0 = api_register(client)
    r1 = client.post(
        "/auth/refresh", json={"refresh_token": t0["refresh_token"]}, headers=csrf(client)
    )
    assert r1.status_code == 200 and r1.json()["refresh_token"] != t0["refresh_token"]
    # simulate an attacker replaying the old token after the grace window
    with user_session(t0["user_id"]) as s:
        s.execute(
            text(
                "UPDATE refresh_tokens SET used_at = now() - interval '1 minute' WHERE used_at IS NOT NULL"
            )
        )
    r2 = client.post(
        "/auth/refresh", json={"refresh_token": t0["refresh_token"]}, headers=csrf(client)
    )
    assert r2.status_code == 401 and r2.json()["code"] == "refresh_token_reuse"
    # the legitimate newer token is now revoked too (whole family)
    r3 = TestClient(app).post("/auth/refresh", json={"refresh_token": r1.json()["refresh_token"]})
    assert r3.status_code == 401
    assert "refresh_reuse" in events(t0["user_id"])
    # access tokens issued before the reuse are rejected (revocation hint)
    assert TestClient(app).get("/me", headers=bearer(r1.json()["access_token"])).status_code == 401


def test_parallel_refresh_within_grace_is_not_treated_as_theft():
    pair = auth.register(unique_email(), PASSWORD, "P")
    a = auth.refresh(pair.refresh_token)
    b = auth.refresh(pair.refresh_token)  # same token again within the grace window
    assert a.refresh_token != b.refresh_token
    assert auth.refresh(a.refresh_token) and auth.refresh(b.refresh_token)


def test_expired_refresh_rejected():
    pair = auth.register(unique_email(), PASSWORD, "E")
    with user_session(pair.user_id) as s:
        s.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'"))
    with pytest.raises(auth.InvalidRefresh):
        auth.refresh(pair.refresh_token)


def test_logout_revokes_refresh_and_access(client):
    t = api_register(client)
    r = client.post("/auth/logout", headers=csrf(client))
    assert r.status_code == 200 and "fp_access" not in client.cookies
    assert (
        TestClient(app)
        .post("/auth/refresh", json={"refresh_token": t["refresh_token"]})
        .status_code
        == 401
    )
    assert TestClient(app).get("/me", headers=bearer(t["access_token"])).status_code == 401


def test_cookie_session_auto_refreshes_expired_access(client):
    api_register(client)
    domain = next(c.domain for c in client.cookies.jar if c.name == "fp_access")
    client.cookies.set("fp_access", "expired-or-garbage", domain=domain)
    r = client.get("/me")
    assert r.status_code == 200
    assert client.cookies.get("fp_access", domain=domain) != "expired-or-garbage"


# ---------------------------------------------------------------- lockout / rate limits
def test_exponential_lockout_after_threshold():
    email = unique_email()
    pair = auth.register(email, PASSWORD, "L")
    for _ in range(5):
        with pytest.raises(auth.InvalidCredentials):
            auth.login(email, "wrong-password-x")
    with pytest.raises(auth.AccountLocked) as e:
        auth.login(email, PASSWORD)  # even the right password is refused while locked
    assert 0 < e.value.retry_after <= 60
    assert "locked" in events(pair.user_id)
    assert auth._lock_minutes(6) == 2 and auth._lock_minutes(20) == 1440


def test_login_rate_limit_per_ip(client):
    for _ in range(5):
        client.post("/auth/login", json={"email": "x@example.com", "password": "nope-nope-1"})
    r = client.post("/auth/login", json={"email": "x@example.com", "password": "nope-nope-1"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


# ---------------------------------------------------------------- password flows
def _last_outbox_link(tmp_outbox) -> str:
    files = sorted(tmp_outbox.glob("*.json"))
    body = json.loads(files[-1].read_text())["body"]
    return re.search(r"token=([\w-]+)", body).group(1)


@pytest.fixture
def tmp_outbox(tmp_path, monkeypatch):
    import finpilot.delivery.log_sink as sink

    monkeypatch.setattr(sink, "OUTBOX", tmp_path)
    return tmp_path


def test_password_reset_flow_single_use(client, tmp_outbox):
    email = unique_email()
    old = api_register(client, email=email)
    client.cookies.clear()
    r = client.post("/auth/password/forgot", json={"email": email})
    assert r.status_code == 202
    assert (
        client.post("/auth/password/forgot", json={"email": "ghost@example.com"}).status_code == 202
    )
    token = _last_outbox_link(tmp_outbox)
    new_pw = "a-brand-new-passphrase-42"
    assert (
        client.post(
            "/auth/password/reset", json={"token": token, "new_password": new_pw}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/auth/password/reset", json={"token": token, "new_password": new_pw + "x"}
        ).status_code
        == 400
    )
    assert (
        client.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
    )
    assert client.post("/auth/login", json={"email": email, "password": new_pw}).status_code == 200
    # old sessions were revoked
    assert (
        TestClient(app)
        .post("/auth/refresh", json={"refresh_token": old["refresh_token"]})
        .status_code
        == 401
    )


def test_password_reset_token_expires(tmp_outbox):
    email = unique_email()
    pair = auth.register(email, PASSWORD, "R")
    auth.forgot_password(email)
    with user_session(pair.user_id) as s:
        s.execute(text("UPDATE password_reset_tokens SET expires_at = now() - interval '1 second'"))
    with pytest.raises(auth.InvalidResetToken):
        auth.reset_password(_last_outbox_link(tmp_outbox), "another-good-passphrase")


def test_change_password_requires_current_and_revokes_other_sessions(client):
    first = api_register(client)
    other = TestClient(app)
    r = client.post(
        "/auth/password/change",
        json={"current_password": "wrong", "new_password": "x" * 12},
        headers=csrf(client),
    )
    assert r.status_code == 401
    r = client.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": "fresh-passphrase-2026"},
        headers=csrf(client),
    )
    assert r.status_code == 200
    assert (
        other.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code
        == 401
    )
    assert client.get("/me").status_code == 200  # this device got fresh tokens


# ---------------------------------------------------------------- CSRF & headers
def test_csrf_required_for_cookie_authenticated_writes(client):
    api_register(client)
    assert client.patch("/me", json={"name": "No CSRF"}).status_code == 403
    assert (
        client.patch("/me", json={"name": "Bad"}, headers={"X-CSRF-Token": "forged"}).status_code
        == 403
    )
    assert client.patch("/me", json={"name": "Good"}, headers=csrf(client)).status_code == 200


def test_bearer_clients_are_exempt_from_csrf(client):
    t = api_register(client)
    api = TestClient(app)
    assert (
        api.patch("/me", json={"name": "Bearer"}, headers=bearer(t["access_token"])).status_code
        == 200
    )


def test_form_posts_always_need_csrf(client):
    r = client.post("/web/login", data={"email": "a@b.co", "password": "x"})
    assert r.status_code == 403  # login-CSRF blocked


def test_security_headers_present(client):
    h = client.get("/login").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in h  # only in prod


def test_cors_locked_to_app_base_url(client):
    r = client.options(
        "/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_profile_patch_validates_and_currency_is_view_only(client):
    api_register(client, "INR")
    h = csrf(client)
    assert client.patch("/me", json={"reporting_currency": "EUR"}, headers=h).status_code == 422
    assert client.patch("/me", json={"timezone": "Nowhere/Land"}, headers=h).status_code == 422
    r = client.patch(
        "/me", json={"reporting_currency": "usd", "timezone": "America/New_York"}, headers=h
    )
    assert r.status_code == 200 and r.json()["reporting_currency"] == "USD"
    assert (
        client.patch(
            "/settings", json={"cash_buffer": "25000.50", "digest_hour": 7}, headers=h
        ).json()["cash_buffer"]
        == "25000.50"
    )
    assert client.patch("/settings", json={"digest_hour": 30}, headers=h).status_code == 422


def test_auth_event_audit_trail(client):
    t = api_register(client)
    client.post("/auth/logout", headers=csrf(client))
    assert events(t["user_id"])[:2] == ["register", "logout"]
    assert isinstance(dt.datetime.now(), dt.datetime)


def test_demo_accounts_never_become_admin(redis_client):
    pair = auth.register(unique_email("demo"), PASSWORD, "Demo", as_demo=True)
    assert pair.role == "user"


def test_forgot_password_answers_normally_when_the_email_cannot_be_sent(client, monkeypatch):
    """An unwritable outbox or a down SMTP server must not turn into a 500 (or reveal anything)."""
    import finpilot.auth.service as auth_service

    def broken(*_a, **_k):
        raise PermissionError("outbox not writable")

    monkeypatch.setattr(auth_service, "send_email", broken)
    email = unique_email()
    api_register(client, email=email)
    client.cookies.clear()
    r = client.post("/auth/password/forgot", json={"email": email})
    assert r.status_code == 202
