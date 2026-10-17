"""JSON auth API. Browser clients also receive httpOnly cookies; API clients use the body."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Request, Response, status

from finpilot.api import schemas as sc
from finpilot.api.cookies import clear_auth_cookies, set_auth_cookies
from finpilot.api.deps import client_info, limit
from finpilot.auth import service as auth
from finpilot.config import get_settings
from finpilot.tenancy import REFRESH_COOKIE, Principal, get_optional_principal, get_principal

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_out(pair: auth.TokenPair) -> sc.TokenOut:
    return sc.TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=get_settings().access_token_ttl_min * 60,
        user_id=pair.user_id,
        role=pair.role,
    )


@router.post("/register", response_model=sc.TokenOut, status_code=status.HTTP_201_CREATED)
def register(body: sc.RegisterIn, request: Request, response: Response) -> sc.TokenOut:
    limit(request, "register", get_settings().rl_register_per_hour_ip, 3600)
    pair = auth.register(
        body.email,
        body.password,
        body.name,
        body.reporting_currency,
        body.timezone,
        client_info(request),
    )
    set_auth_cookies(response, pair)
    return _token_out(pair)


@router.post("/login", response_model=sc.TokenOut)
def login(body: sc.LoginIn, request: Request, response: Response) -> sc.TokenOut:
    limit(request, "login", get_settings().rl_login_per_min_ip, 60)
    pair = auth.login(body.email, body.password, client_info(request))
    from finpilot.tasks.pipeline_tasks import schedule_prewarm

    schedule_prewarm(pair.user_id)  # stale pre-warmed answers → refresh in the background
    set_auth_cookies(response, pair)
    return _token_out(pair)


@router.post("/refresh", response_model=sc.TokenOut)
def refresh(request: Request, response: Response, body: sc.RefreshIn | None = None) -> sc.TokenOut:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE, "")
    try:
        pair = auth.refresh(token, client_info(request))
    except auth.InvalidRefresh:
        clear_auth_cookies(response)
        raise
    set_auth_cookies(response, pair)
    return _token_out(pair)


@router.post("/logout", response_model=sc.Message)
def logout(
    request: Request,
    response: Response,
    body: sc.RefreshIn | None = None,
    p: Principal | None = Depends(get_optional_principal),
) -> sc.Message:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    auth.logout(token, p.jti if p else None, client_info(request))
    clear_auth_cookies(response)
    return sc.Message(detail="Logged out")


@router.post("/password/forgot", response_model=sc.Message, status_code=status.HTTP_202_ACCEPTED)
def forgot(body: sc.ForgotIn, request: Request) -> sc.Message:
    s = get_settings()
    limit(request, "pwreset_ip", s.rl_reset_per_15min * 3, 900)
    email_key = hashlib.sha256(body.email.strip().lower().encode()).hexdigest()[:24]
    limit(request, "pwreset_email", s.rl_reset_per_15min, 900, key=email_key)
    auth.forgot_password(body.email, client_info(request))
    return sc.Message(detail="If an account exists for that email, a reset link has been sent.")


@router.post("/password/reset", response_model=sc.Message)
def reset(body: sc.ResetIn, request: Request) -> sc.Message:
    limit(request, "pwreset_ip", get_settings().rl_reset_per_15min * 3, 900)
    auth.reset_password(body.token, body.new_password, client_info(request))
    return sc.Message(detail="Password updated. Please sign in.")


@router.post("/password/change", response_model=sc.TokenOut)
def change_password(
    body: sc.ChangePasswordIn,
    request: Request,
    response: Response,
    p: Principal = Depends(get_principal),
) -> sc.TokenOut:
    pair = auth.change_password(
        p.user_id, body.current_password, body.new_password, client_info(request)
    )
    set_auth_cookies(response, pair)
    return _token_out(pair)
