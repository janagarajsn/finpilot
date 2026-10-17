"""Server-rendered UI: pages render, auth redirects, htmx form flows with CSRF."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finpilot.api.main import app
from tests.conftest import needs_pg, needs_redis
from tests.helpers import PASSWORD, unique_email

pytestmark = [needs_pg, needs_redis]
HX = {"HX-Request": "true"}


@pytest.fixture
def web(redis_client):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def form_csrf(c: TestClient) -> str:
    if "fp_csrf" not in c.cookies:
        c.get("/login")
    return c.cookies["fp_csrf"]


def signup(c: TestClient, currency: str = "INR") -> str:
    email = unique_email("web")
    r = c.post(
        "/web/register",
        headers=HX,
        data={
            "csrf_token": form_csrf(c),
            "name": "Web User",
            "email": email,
            "password": PASSWORD,
            "reporting_currency": currency,
            "timezone": "Asia/Kolkata",
        },
    )
    assert r.headers.get("HX-Redirect") == "/onboarding", r.text[:500]
    return email


@pytest.mark.parametrize(
    "path", ["/login", "/register", "/forgot-password", "/reset-password?token=x"]
)
def test_public_pages_render(web, path):
    r = web.get(path)
    assert r.status_code == 200 and "FinPilot" in r.text and 'name="csrf_token"' in r.text
    assert "<script>" not in r.text  # no inline scripts (CSP)


@pytest.mark.parametrize("path", ["/", "/onboarding", "/account"])
def test_app_pages_redirect_when_logged_out(web, path):
    r = web.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signup_onboarding_dashboard_flow(web):
    signup(web, "USD")
    assert web.get("/").headers["location"] == "/onboarding"
    page = web.get("/onboarding")
    assert page.status_code == 200 and "USD" in page.text
    r = web.post(
        "/web/onboarding",
        headers=HX,
        data={
            "csrf_token": form_csrf(web),
            "reporting_currency": "INR",
            "timezone": "Asia/Kolkata",
        },
    )
    assert r.headers["HX-Redirect"] == "/"
    dash = web.get("/")
    assert dash.status_code == 200 and "Get started" in dash.text and "₹0.00" in dash.text
    assert "Amounts shown in INR" in dash.text


def test_form_errors_render_inline(web):
    r = web.post(
        "/web/register",
        headers=HX,
        data={
            "csrf_token": form_csrf(web),
            "name": "X",
            "email": unique_email(),
            "password": "password123",
        },
    )
    assert r.status_code == 200 and "too common" in r.text and 'id="register-form"' in r.text
    r = web.post(
        "/web/login",
        headers=HX,
        data={"csrf_token": form_csrf(web), "email": "no@example.com", "password": "whatever-123"},
    )
    assert "Incorrect email or password" in r.text


def test_login_logout_via_forms(web):
    email = signup(web)
    web.post("/web/logout", data={"csrf_token": form_csrf(web)})
    assert "fp_access" not in web.cookies
    r = web.post(
        "/web/login", data={"csrf_token": form_csrf(web), "email": email, "password": PASSWORD}
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "fp_access" in web.cookies


def test_account_settings_forms(web):
    signup(web)
    tok = form_csrf(web)
    r = web.post(
        "/web/account/profile",
        headers=HX,
        data={
            "csrf_token": tok,
            "name": "Renamed",
            "reporting_currency": "USD",
            "timezone": "Europe/London",
        },
    )
    assert "Profile saved" in r.text and "Renamed" in r.text
    r = web.post(
        "/web/account/settings",
        headers=HX,
        data={
            "csrf_token": tok,
            "digest_enabled": "1",
            "digest_channel": "email",
            "digest_dow": "1",
            "digest_hour": "8",
            "cash_buffer": "-5",
        },
    )
    assert "check the values" in r.text
    r = web.post(
        "/web/account/password",
        headers=HX,
        data={"csrf_token": tok, "current_password": "nope", "new_password": "irrelevant-123"},
    )
    assert "Current password is incorrect" in r.text


def test_unknown_page_renders_html_404(web):
    r = web.get("/does-not-exist", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Back to FinPilot" in r.text
