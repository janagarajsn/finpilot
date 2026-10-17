"""CSRF protection: double-submit cookie.

A random token lives in the `fp_csrf` cookie (readable by our JS) and must be echoed in the
`X-CSRF-Token` header (htmx/fetch) or a `csrf_token` form field. Required on unsafe methods when
the request carries our auth cookies, and ALWAYS for form submissions (blocks login-CSRF).
Bearer-token API clients and cookie-less JSON clients are exempt: browsers cannot attach
cookies or send cross-site JSON without a CORS preflight, and CORS is locked to APP_BASE_URL.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

CSRF_COOKIE = "fp_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
AUTH_COOKIES = ("fp_access", "fp_refresh")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_token_for(request: Request) -> str:
    """Token to embed in pages: existing cookie value, or the one the middleware will set."""
    return request.cookies.get(CSRF_COOKIE) or request.state.__dict__.setdefault(
        "new_csrf", new_csrf_token()
    )


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    ctype = request.headers.get("content-type", "")
    is_form = ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
    has_auth_cookie = any(c in request.cookies for c in AUTH_COOKIES)
    if not (is_form or has_auth_cookie):
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.headers.get(CSRF_HEADER, "")
    if not sent and is_form:
        form = await request.form()
        sent = str(form.get(CSRF_FIELD, ""))
    if not cookie or not sent or not hmac.compare_digest(cookie, sent):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
