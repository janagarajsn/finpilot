"""Auth/CSRF cookie helpers. httpOnly auth cookies; Secure in prod; SameSite=Lax."""

from __future__ import annotations

from starlette.responses import Response

from finpilot.auth.service import TokenPair
from finpilot.config import get_settings
from finpilot.security.csrf import CSRF_COOKIE
from finpilot.tenancy import ACCESS_COOKIE, REFRESH_COOKIE


def set_auth_cookies(resp: Response, pair: TokenPair) -> None:
    s = get_settings()
    resp.set_cookie(
        ACCESS_COOKIE,
        pair.access_token,
        max_age=s.access_token_ttl_min * 60,
        httponly=True,
        secure=s.is_prod,
        samesite="lax",
        path="/",
    )
    resp.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=s.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=s.is_prod,
        samesite="lax",
        path="/",
    )


def clear_auth_cookies(resp: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        resp.delete_cookie(name, path="/")


def set_csrf_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=7 * 86400,
        httponly=False,
        secure=get_settings().is_prod,
        samesite="strict",
        path="/",
    )
