"""Server-rendered pages (Jinja + htmx). Form posts call the same services as the JSON API.

htmx forms use `hx-select` on the form id, so handlers simply re-render the full page with an
error/notice; on success they send `HX-Redirect` (or a 303 for non-JS clients).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
import zoneinfo
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from finpilot.agents import advisor, coordinator, goal_tracker
from finpilot.agents.analyzer import get_report
from finpilot.api.cookies import clear_auth_cookies, set_auth_cookies
from finpilot.api.deps import client_info, limit
from finpilot.auth import service as auth
from finpilot.categories import service as categories
from finpilot.config import get_settings
from finpilot.db.repos import users as repo
from finpilot.db.session import user_session
from finpilot.digests import alerts as alerts_svc
from finpilot.digests import service as digests_svc
from finpilot.forecasting import goals as goals_mod
from finpilot.forecasting.service import get_forecast
from finpilot.ingest import import_service as imp
from finpilot.ingest import sample_data
from finpilot.ingest import template as import_template
from finpilot.insights import dashboard as dash
from finpilot.money import format_money, get_currency_registry
from finpilot.recurring import detected
from finpilot.recurring import service as rec
from finpilot.security.csrf import csrf_token_for
from finpilot.security.ratelimit import RateLimited
from finpilot.tenancy import REFRESH_COOKIE, Principal, get_optional_principal
from finpilot.transactions import service as txns

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = lambda v, cur, locale=None: format_money(v, cur, locale)


def _day(v: Any) -> str:
    """Dates shown by the tool: '15 Oct' this year, '15 Oct 2025' otherwise."""
    if v is None or v == "":
        return ""
    d = v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])
    return d.strftime("%d %b") if d.year == dt.date.today().year else d.strftime("%d %b %Y")


templates.env.filters["day"] = _day
templates.env.filters["month"] = lambda v: dt.date.fromisoformat(str(v)[:7] + "-01").strftime(
    "%b %Y"
)
# tojson (used for JSON blobs and x-data) must handle UUID / Decimal / dates.
templates.env.policies["json.dumps_kwargs"] = {
    "default": str,
    "sort_keys": False,
    "ensure_ascii": False,
}

NAV: dict[str, Any] = {
    # grouped sidebar: a top-level link has `href`; a group has `children` [(href, label)]
    "main": [
        {"label": "Overview", "href": "/", "icon": "home"},
        {
            "label": "Money",
            "icon": "wallet",
            "children": [
                ("/app/transactions", "Transactions"),
                ("/app/recurring", "Recurring"),
                ("/app/categories", "Categories"),
            ],
        },
        {
            "label": "Planning",
            "icon": "chart",
            "children": [
                ("/app/forecast", "Forecast & goals"),
                ("/app/advice", "Advice"),
                ("/app/insights", "Insights"),
            ],
        },
        {
            "label": "Assistant",
            "icon": "chat",
            "children": [("/app/chat", "Chat"), ("/app/memory", "Memory")],
        },
    ],
    # (href, label, icon, admin only)
    "tools": [
        ("/app/import", "Import", "upload", False),
        ("/app/pipeline", "Pipeline", "flow", True),
        ("/app/eval", "Quality", "check", True),
        ("/account", "Settings", "gear", False),
    ],
}
SOURCE_LABELS = {
    "mcp": "live (FX MCP)",
    "fallback_api": "live (direct)",
    "stale_db": "stored rates (live source unavailable)",
    "pinned": "approximate",
    "synthetic": "synthetic",
}


@lru_cache
def _timezones() -> list[str]:
    return sorted(
        z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith("Etc/")
    )


def _currencies() -> list[Any]:
    reg = get_currency_registry()
    return [reg.get(c) for c in reg.codes]


def _fx() -> Any:
    from finpilot.api.routes_fx import fx_service

    try:
        return fx_service()
    except Exception:
        log.warning("fx service unavailable", exc_info=True)
        return None


def _fx_status() -> dict[str, Any] | None:
    try:
        from finpilot.api.routes_fx import fx_service

        st = fx_service().status().as_dict()
        st["source_label"] = SOURCE_LABELS.get(st["source"] or "", st["source"])
        return st
    except Exception:
        log.warning("fx status unavailable", exc_info=True)
        return None


def _page(
    request: Request, name: str, *, p: Principal | None = None, status: int = 200, **ctx: Any
) -> HTMLResponse:
    user, ctx_blob, inbox = None, None, None
    if p is not None:
        with user_session(p.user_id) as s:
            user = repo.get_profile(s, p.user_id)
            inbox = digests_svc.unread_counts(s, p.user_id)
            visible = [c for c in categories.list_categories(s, p.user_id) if not c["hidden"]]
            ctx_blob = {
                "reporting_currency": user["reporting_currency"],
                "currencies": get_currency_registry().codes,
                "categories": [
                    {k: c[k] for k in ("id", "name", "flow_type", "icon", "color")} for c in visible
                ],
                "accounts": [
                    {k: a[k] for k in ("id", "name", "currency")}
                    for a in repo.list_accounts(s, p.user_id)
                    if not a["archived"]
                ],
            }
    base = {
        "ctx": ctx_blob,
        "user": user,
        "csrf_token": csrf_token_for(request),
        "nav": NAV,
        "active": request.url.path,
        "fx": _fx_status() if user else None,
        "inbox": inbox,
        "currencies": _currencies(),
        "timezones": _timezones(),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)


def _admin_required(request: Request) -> Principal | Response:
    """Pipeline and Quality are operator screens: admins only (others get a 404 page)."""
    p = _login_required(request)
    if isinstance(p, Response) or p.role == "admin":
        return p
    return _page(request, "error.html", p=p, status=404, message="Page not found.")


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _login_required(request: Request) -> Principal | Response:
    p = get_optional_principal(request)
    if p is None:
        return RedirectResponse("/login", status_code=303)
    return p


# ---------------------------------------------------------------------------- auth pages
@router.get("/login")
def login_page(request: Request, reset: int = 0, deleted: int = 0) -> Response:
    if get_optional_principal(request):
        return RedirectResponse("/", status_code=303)
    notice = (
        "Your account and all its data were deleted."
        if deleted
        else "Password updated. Please sign in."
        if reset
        else None
    )
    return _page(request, "login.html", notice_msg=notice)


@router.post("/web/login")
def login_submit(request: Request, email: str = Form(""), password: str = Form("")) -> Response:
    try:
        limit(request, "login", get_settings().rl_login_per_min_ip, 60)
        pair = auth.login(email, password, client_info(request))
    except RateLimited as e:
        return _page(
            request,
            "login.html",
            email=email,
            status=429,
            error_msg=f"Too many attempts. Try again in {e.retry_after}s.",
        )
    except auth.AuthError as e:
        return _page(request, "login.html", email=email, error_msg=str(e))
    from finpilot.tasks.pipeline_tasks import schedule_prewarm

    schedule_prewarm(pair.user_id)  # stale pre-warmed answers → refresh in the background
    resp = _redirect(request, "/")
    set_auth_cookies(resp, pair)
    return resp


@router.get("/register")
def register_page(request: Request) -> Response:
    if get_optional_principal(request):
        return RedirectResponse("/", status_code=303)
    return _page(request, "register.html", form={})


@router.post("/web/register")
def register_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    reporting_currency: str = Form("INR"),
    timezone: str = Form("Asia/Kolkata"),
) -> Response:
    form = {
        "name": name,
        "email": email,
        "reporting_currency": reporting_currency,
        "timezone": timezone,
    }
    try:
        limit(request, "register", get_settings().rl_register_per_hour_ip, 3600)
        pair = auth.register(
            email,
            password,
            name,
            reporting_currency,
            timezone or "Asia/Kolkata",
            client_info(request),
        )
    except RateLimited as e:
        return _page(
            request,
            "register.html",
            form=form,
            status=429,
            error_msg=f"Too many sign-ups from your network. Try again in {e.retry_after}s.",
        )
    except auth.AuthError as e:
        return _page(request, "register.html", form=form, error_msg=str(e))
    resp = _redirect(request, "/onboarding")
    set_auth_cookies(resp, pair)
    return resp


@router.get("/forgot-password")
def forgot_page(request: Request) -> Response:
    return _page(request, "forgot.html")


@router.post("/web/forgot")
def forgot_submit(request: Request, email: str = Form("")) -> Response:
    s = get_settings()
    try:
        limit(request, "pwreset_ip", s.rl_reset_per_15min * 3, 900)
        auth.forgot_password(email, client_info(request))
    except RateLimited as e:
        return _page(
            request,
            "forgot.html",
            status=429,
            error_msg=f"Too many requests. Try again in {e.retry_after}s.",
        )
    return _page(
        request,
        "forgot.html",
        notice_msg="If an account exists for that email, a reset link is on its way.",
    )


@router.get("/reset-password")
def reset_page(request: Request, token: str = "") -> Response:
    return _page(request, "reset.html", token=token)


@router.post("/web/reset")
def reset_submit(request: Request, token: str = Form(""), new_password: str = Form("")) -> Response:
    try:
        limit(request, "pwreset_ip", get_settings().rl_reset_per_15min * 3, 900)
        auth.reset_password(token, new_password, client_info(request))
    except RateLimited as e:
        return _page(
            request,
            "reset.html",
            token=token,
            status=429,
            error_msg=f"Too many attempts. Try again in {e.retry_after}s.",
        )
    except auth.AuthError as e:
        return _page(request, "reset.html", token=token, error_msg=str(e))
    return _redirect(request, "/login?reset=1")


@router.post("/web/logout")
def logout_submit(request: Request) -> Response:
    p = get_optional_principal(request)
    auth.logout(request.cookies.get(REFRESH_COOKIE), p.jti if p else None, client_info(request))
    resp = _redirect(request, "/login")
    clear_auth_cookies(resp)
    return resp


# ---------------------------------------------------------------------------- app pages
def _checklist(user: dict[str, Any], has_txns: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "title": "Create your account",
            "detail": "Done. Welcome aboard.",
            "done": True,
            "available": True,
        },
        {
            "title": "Choose your reporting currency and timezone",
            "detail": f"Currently {user['reporting_currency']} · {user['timezone']}.",
            "done": user["onboarded_at"] is not None,
            "available": True,
        },
        {
            "title": "Add transactions",
            "href": "/app/import",
            "detail": "Import the CSV/XLSX template or your bank export, or press + Add expense.",
            "done": has_txns,
            "available": True,
        },
        {
            "title": "Declare recurring bills and income",
            "href": "/app/recurring",
            "detail": "Rent, EMIs, subscriptions, salary: they drive the forecast.",
            "done": False,
            "available": True,
        },
        {
            "title": "Create your first goal",
            "detail": "Save for a trip, cap a category, or pay off debt.",
            "done": False,
            "available": False,
        },
    ]


@router.get("/")
def dashboard(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        user = repo.get_profile(s, p.user_id)
    if user["onboarded_at"] is None:
        return RedirectResponse("/onboarding", status_code=303)
    period = request.query_params.get("period", "this_month")
    with user_session(p.user_id) as s:
        get_report(s, p.user_id)  # first: refreshes detected repeats for "upcoming"
        d = dash.build(s, p.user_id, period if period in dash.PERIODS else "this_month", _fx())
        fc = get_forecast(s, p.user_id, fx=_fx()) if d["counts"]["transactions"] else None
        recs = advisor.list_recommendations(s, p.user_id)
        runs1 = coordinator.list_runs(s, p.user_id, 1)
        last_run = runs1[0] if runs1 else None
        alerts = alerts_svc.list_alerts(s, p.user_id, unread_only=True, limit=1)
        sample = _sample(s, p.user_id)
    chart = {
        "reporting_currency": d["reporting_currency"],
        "trend": d["trend"],
        "breakdown": [{k: b[k] for k in ("name", "color", "spend")} for b in d["breakdown"]],
    }
    return _page(
        request,
        "dashboard.html",
        p=p,
        d=d,
        chart=chart,
        periods=dash.PERIODS,
        checklist=_checklist(user, d["counts"]["transactions"] > 0),
        sample=sample,
        fc=fc,
        month_end=fc.month_ends[0] if fc and fc.month_ends else None,
        recs=recs,
        last_run=last_run,
        alerts=alerts,
    )


def _sample(s: Any, user_id: uuid.UUID) -> dict[str, Any]:
    return {"datasets": sample_data.datasets(), "loaded": sample_data.loaded_dataset(s, user_id)}


@router.get("/onboarding")
def onboarding_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    return _page(request, "onboarding.html", p=p)


@router.post("/web/onboarding")
def onboarding_submit(
    request: Request, reporting_currency: str = Form("INR"), timezone: str = Form("")
) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    try:
        auth.validate_profile(reporting_currency.upper(), timezone)
    except auth.AuthError as e:
        return _page(request, "onboarding.html", p=p, error_msg=str(e))
    with user_session(p.user_id) as s:
        repo.update_profile(
            s, p.user_id, {"reporting_currency": reporting_currency.upper(), "timezone": timezone}
        )
        repo.mark_onboarded(s, p.user_id)
    return _redirect(request, "/")


def _account(request: Request, p: Principal, **ctx: Any) -> HTMLResponse:
    with user_session(p.user_id) as s:
        settings = repo.get_settings_row(s, p.user_id)
    return _page(request, "account.html", p=p, settings=settings, **ctx)


@router.get("/account")
def account_page(request: Request) -> Response:
    p = _login_required(request)
    return p if isinstance(p, Response) else _account(request, p)


@router.post("/web/account/profile")
def account_profile(
    request: Request,
    name: str = Form(""),
    reporting_currency: str = Form(""),
    timezone: str = Form(""),
) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    name = name.strip()
    try:
        if not 1 <= len(name) <= 100:
            raise auth.InvalidInput("Name is required (max 100 characters).")
        auth.validate_profile(reporting_currency.upper(), timezone)
    except auth.AuthError as e:
        return _account(request, p, profile_error=str(e))
    with user_session(p.user_id) as s:
        repo.update_profile(
            s,
            p.user_id,
            {"name": name, "reporting_currency": reporting_currency.upper(), "timezone": timezone},
        )
    return _account(request, p, profile_notice="Profile saved.")


@router.post("/web/account/password")
def account_password(
    request: Request, current_password: str = Form(""), new_password: str = Form("")
) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    try:
        pair = auth.change_password(p.user_id, current_password, new_password, client_info(request))
    except auth.AuthError as e:
        return _account(request, p, pw_error=str(e))
    resp = _account(request, p, pw_notice="Password changed. Other sessions were signed out.")
    set_auth_cookies(resp, pair)
    return resp


@router.post("/web/account/delete")
def account_delete(request: Request, password: str = Form(""), confirm: str = Form("")) -> Response:
    from finpilot.account_data import delete_account

    p = _login_required(request)
    if isinstance(p, Response):
        return p
    if confirm != "DELETE":
        return _account(request, p, del_error="Type DELETE to confirm.")
    try:
        delete_account(p.user_id, password, client_info(request))
    except auth.AuthError as e:
        return _account(request, p, del_error=str(e))
    resp = _redirect(request, "/login?deleted=1")
    clear_auth_cookies(resp)
    return resp


@router.post("/web/account/settings")
def account_settings(
    request: Request,
    digest_enabled: str = Form(""),
    digest_channel: str = Form("in_app"),
    digest_dow: int = Form(0),
    digest_hour: int = Form(9),
    risk_alert_enabled: str = Form(""),
    cash_buffer: str = Form(""),
) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    try:
        buffer = Decimal(cash_buffer) if cash_buffer.strip() else None
        if buffer is not None and (buffer < 0 or not buffer.is_finite()):
            raise InvalidOperation
        if (
            digest_channel not in ("in_app", "email", "telegram")
            or not (0 <= digest_dow <= 6)
            or not (0 <= digest_hour <= 23)
        ):
            raise ValueError
    except (InvalidOperation, ValueError):
        return _account(request, p, settings_error="Please check the values you entered.")
    with user_session(p.user_id) as s:
        repo.update_settings_row(
            s,
            p.user_id,
            {
                "digest_enabled": bool(digest_enabled),
                "digest_channel": digest_channel,
                "digest_dow": digest_dow,
                "digest_hour": digest_hour,
                "risk_alert_enabled": bool(risk_alert_enabled),
                "cash_buffer": buffer,
            },
        )
    return _account(request, p, settings_notice="Preferences saved.")


# ---------------------------------------------------------------------------- ledger pages
def _uuid_or_none(v: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(v) if v else None
    except ValueError:
        return None


def _date_or_none(v: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        return None


@router.get("/app/transactions")
def transactions_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    q = request.query_params
    cat = q.get("category") or ""
    kind = q.get("kind") if q.get("kind") in ("expense", "income") else None
    sort = q.get("sort") or "date_desc"
    sort = sort if sort in txns.SORTS else "date_desc"
    try:
        offset = max(0, int(q.get("offset", "0")))
    except ValueError:
        offset = 0
    date_from, date_to = _date_or_none(q.get("date_from")), _date_or_none(q.get("date_to"))
    # A preset period wins over the From/To dates (which the form also sends); "Any time" clears
    # them; links that carry only dates (no period) are a custom range.
    period = q.get("period")
    if period is None:
        period = "custom" if date_from or date_to else ""
    elif period not in txns.PERIODS:
        period = ""
    if period == "":
        date_from = date_to = None
    elif period != "custom":
        with user_session(p.user_id) as s:
            tz = repo.get_profile(s, p.user_id)["timezone"]
        date_from, date_to = txns.period_range(period, rec.local_today(tz)) or (None, None)
    f = txns.TxnFilter(
        q=(q.get("q") or "")[:100] or None,
        category_id=_uuid_or_none(cat),
        uncategorized=cat == "uncategorized",
        kind=kind,  # type: ignore[arg-type]
        date_from=date_from,
        date_to=date_to,
        needs_review=True if q.get("needs_review") == "true" else None,
        import_batch_id=_uuid_or_none(q.get("import_batch_id")),
        ids=[u for u in (_uuid_or_none(x) for x in (q.get("ids") or "").split(",")[:100]) if u]
        or None,
        sort=sort,
        limit=50,
        offset=offset,
    )
    with user_session(p.user_id) as s:
        items, total = txns.list_(s, p.user_id, f)
        review = s.execute(
            text("SELECT count(*) FROM transactions WHERE user_id = :u AND needs_review"),
            {"u": p.user_id},
        ).scalar_one()
        cats = [c for c in categories.list_categories(s, p.user_id) if not c["hidden"]]

    def page_url(off: int) -> str:
        params = {k: v for k, v in q.items() if k != "offset"}
        params["offset"] = str(max(0, off))
        return "/app/transactions?" + urlencode(params)

    filtered = any(
        q.get(k)
        for k in (
            "q",
            "category",
            "kind",
            "period",
            "date_from",
            "date_to",
            "needs_review",
            "import_batch_id",
            "ids",
        )
    )
    return _page(
        request,
        "transactions.html",
        p=p,
        items=items,
        total=total,
        f=f,
        period=period,
        periods=txns.PERIODS,
        categories=cats,
        review_count=review,
        page_url=page_url,
        filtered=filtered,
    )


@router.get("/app/categories")
def categories_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        cats = categories.list_categories(s, p.user_id)
    return _page(request, "categories.html", p=p, categories=cats)


@router.get("/app/recurring")
def recurring_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        get_report(s, p.user_id)  # refreshes detection if transactions changed
        rules = rec.list_rules(s, p.user_id)
        suggestions = detected.list_detected(s, p.user_id)
    return _page(request, "recurring.html", p=p, rules=rules, suggestions=suggestions)


@router.get("/app/forecast")
def forecast_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        get_report(s, p.user_id)  # detected repeats feed the forecast
        fc = get_forecast(
            s, p.user_id, fx=_fx(), refresh=request.query_params.get("refresh") == "1"
        )
        goals = goals_mod.list_goals(s, p.user_id)
        history = {str(g["id"]): goal_tracker.history(s, p.user_id, g["id"], 8) for g in goals}
        cats = [
            c
            for c in categories.list_categories(s, p.user_id)
            if not c["hidden"] and c["flow_type"] == "expense"
        ]
        subs = [
            {
                "id": str(d["id"]),
                "merchant": d["merchant_norm"].title(),
                "amount": str(d["amount_typical"]),
                "currency": d["currency"],
            }
            for d in detected.list_detected(s, p.user_id, include_all=True)
            if d["kind"] == "expense" and d["status"] == "active" and not d["dismissed"]
        ]
    data = {
        "currency": fc.currency,
        "dates": [d.isoformat() for d in fc.dates],
        "p10": fc.p10,
        "p50": fc.p50,
        "p90": fc.p90,
        "buffer": float(fc.buffer),
        "subscriptions": subs,
        "categories": [{"id": str(c["id"]), "name": c["name"]} for c in cats],
        "today": fc.as_of.isoformat(),
    }
    return _page(
        request, "forecast.html", p=p, fc=fc, goals=goals, data=data, cats=cats, history=history
    )


@router.get("/app/advice")
def advice_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        recs = advisor.list_recommendations(s, p.user_id)
        has_txns = s.execute(
            text("SELECT 1 FROM transactions WHERE user_id = :u LIMIT 1"), {"u": p.user_id}
        ).first()
    st = get_settings()
    return _page(
        request,
        "advice.html",
        p=p,
        recs=recs,
        has_txns=bool(has_txns),
        langfuse_base=f"{st.langfuse_host}/project/{st.langfuse_init_project_id}"
        if p.role == "admin"
        else None,
    )


@router.get("/app/pipeline")
def pipeline_page(request: Request) -> Response:
    p = _admin_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        runs = coordinator.list_runs(s, p.user_id, 20)
        sel = _uuid_or_none(request.query_params.get("run")) or (runs[0]["id"] if runs else None)
        run = coordinator.get_run(s, p.user_id, sel) if sel else None
    st = get_settings()
    return _page(
        request,
        "pipeline.html",
        p=p,
        runs=runs,
        run=run,
        steps_order=coordinator.PIPELINE,
        langfuse_base=f"{st.langfuse_host}/project/{st.langfuse_init_project_id}",
        jobs=_jobs(),
    )


def _jobs() -> list[dict[str, Any]] | None:
    from finpilot.tasks.jobs import status

    try:
        return status(recent=1)
    except Exception:
        log.warning("scheduler status unavailable", exc_info=True)
        return None


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import json as _json

    return list(_json.loads(_json.dumps(rows, default=str)))


@router.get("/app/chat")
def chat_page(request: Request) -> Response:
    from finpilot.agents import chat_agent

    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        sessions = chat_agent.list_sessions(s, p.user_id)
        sel = _uuid_or_none(request.query_params.get("session"))
        current = None
        if sel:
            try:
                current = chat_agent.get_session(s, p.user_id, sel)
            except chat_agent.ChatError:
                current = None
    messages = [
        {
            "id": str(m["id"]),
            "role": m["role"],
            "content": m["content"],
            "route": m["route"],
            "cache_layer": m["cache_layer"],
            "feedback": m["feedback"],
            "tools": [t.get("name") for t in (m["tools_json"] or [])],
        }
        for m in (current or {}).get("messages", [])
    ]
    return _page(
        request,
        "chat.html",
        p=p,
        sessions=sessions,
        current=current,
        chat_state={"session_id": str(current["id"]) if current else None, "messages": messages},
    )


@router.get("/app/inbox")
def inbox_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        items = digests_svc.list_digests(s, p.user_id)
        sel = _uuid_or_none(request.query_params.get("digest")) or (
            items[0]["id"] if items else None
        )
        current = digests_svc.get_digest(s, p.user_id, sel) if sel else None
        alerts = alerts_svc.list_alerts(s, p.user_id)
        prefs = (
            s.execute(
                text(
                    "SELECT digest_enabled, digest_channel, digest_dow, digest_hour FROM user_settings WHERE user_id = :u"
                ),
                {"u": p.user_id},
            )
            .mappings()
            .first()
        )
    return _page(
        request, "inbox.html", p=p, digests=items, current=current, alerts=alerts, prefs=prefs
    )


@router.get("/app/memory")
def memory_page(request: Request) -> Response:
    from finpilot.memory.long_term import PgMemory

    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        rows = PgMemory().list_all(s, p.user_id, include_inactive=True)
        pending = s.execute(
            text("""SELECT count(*) FROM chat_sessions WHERE user_id = :u
                    AND (consolidated_at IS NULL OR consolidated_at < last_message_at)"""),
            {"u": p.user_id},
        ).scalar_one()
    return _page(request, "memory.html", p=p, memories=_jsonable(rows), pending=pending)


@router.get("/app/insights")
def insights_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        report = get_report(s, p.user_id, refresh=request.query_params.get("refresh") == "1")
    charts = {
        "currency": report.reporting_currency,
        "weekly": [
            {"week": w["week_ending"], "spend": float(w["spend"])} for w in report.weekly_spend
        ],
        "savings": (report.patterns.get("savings_rate") or {}).get("months", []),
    }
    return _page(request, "insights.html", p=p, r=report, charts=charts)


@router.get("/app/import")
def import_page(request: Request) -> Response:
    p = _login_required(request)
    if isinstance(p, Response):
        return p
    with user_session(p.user_id) as s:
        profile = repo.get_profile(s, p.user_id)
        visible = [c for c in categories.list_categories(s, p.user_id) if not c["hidden"]]
        schema = import_template.schema(
            visible, get_currency_registry().codes, profile["reporting_currency"]
        )
        batches = imp.list_batches(s, p.user_id)
        sample = _sample(s, p.user_id)
    return _page(request, "import.html", p=p, schema=schema, batches=batches, sample=sample)


@router.get("/app/settings")
def settings_alias(request: Request) -> Response:
    return RedirectResponse("/account", status_code=307)


EVAL_KPIS = [
    ("categorization.accuracy", "Accuracy", "Share of transactions categorized correctly"),
    ("categorization.macro_f1", "Macro-F1", "Average F1 across categories"),
    ("categorization.coverage", "Coverage", "Share of transactions the system answered"),
    (
        "categorization.precision_resolved",
        "Precision when answering",
        "Accuracy among answered transactions",
    ),
]


# Evaluation screen: tab → (title, suites whose cases the failures explorer can show)
SUITE_TABS = [
    ("categorization", "Categorization"),
    ("anomalies", "Anomalies"),
    ("recurring", "Recurring"),
    ("forecast", "Forecast"),
    ("simulation", "What-if"),
    ("advisor", "Advisor"),
    ("cache", "Cache"),
    ("memory", "Memory & chat"),
    ("fx", "FX & MCP"),
    ("guardrails", "Guardrails"),
]
TREND_METRICS = [
    ("categorization.accuracy", "Categorization accuracy"),
    ("anomalies.recall", "Anomaly recall"),
    ("forecast.coverage_daily", "Forecast band coverage"),
    ("advisor.pct_grounded", "Advice grounded"),
    ("cache.hit_rate", "Cache hit rate"),
    ("memory.hit_at_5", "Memory hit@5"),
    ("fx.conversion_accuracy", "FX conversions"),
]


def _eval_trend(all_runs: list[dict[str, Any]], mode: str, n: int = 15) -> dict[str, Any]:
    """Key headline metrics over the last `n` runs of the same mode (oldest first)."""
    rows = [r for r in all_runs if r["mode"] == mode and r.get("headline")][:n][::-1]
    return {
        "labels": [r["started_at"].strftime("%d %b %H:%M") for r in rows],
        "series": [
            {"label": label, "data": [(r["headline"] or {}).get(key) for r in rows]}
            for key, label in TREND_METRICS
            if any((r["headline"] or {}).get(key) is not None for r in rows)
        ],
    }


@router.get("/app/eval")
def eval_page(request: Request) -> Response:
    p = _admin_required(request)
    if isinstance(p, Response):
        return p
    from finpilot.api.routes_eval import run_detail, runs

    all_runs = runs(limit=50, _=p)
    run_id = _uuid_or_none(request.query_params.get("run")) or (
        all_runs[0]["id"] if all_runs else None
    )
    run = run_detail(run_id) if run_id else None
    cat: dict[str, Any] = {}
    personas, cost = [], []
    if run:
        details = (run["summary_json"].get("details") or {}).get("categorization") or {}
        primary = details.get("primary_variant")
        cat = {
            "variants": details.get("variants", {}),
            "per_category": details.get("per_category", []),
            "confusions": details.get("confusions", []),
            "n_cases": (run["summary_json"].get("headline") or {}).get("categorization.n_cases", 0),
        }
        personas = [
            {"persona": m["dimension"]["persona"], "value": m["value"]}
            for m in run["metrics"]
            if m["metric"] == "accuracy"
            and m["dimension"].get("scope") == "persona"
            and m["dimension"].get("variant") == primary
        ]
        fmt = {
            "llm_cold_calls_per_1k": ("LLM calls / 1k txns", "{:.2f}"),
            "llm_cold_cost_usd_per_1k": ("LLM cost / 1k txns (uncached)", "${:.4f}"),
            "cached_token_share": ("Prompt tokens served from provider cache", "{:.0%}"),
        }
        cost = [
            {"label": fmt[m["metric"]][0], "text": fmt[m["metric"]][1].format(m["value"])}
            for m in run["metrics"]
            if m["metric"] in fmt and m["dimension"].get("variant") == "full"
        ]
        chart = {
            "run_id": str(run["id"]),
            "calibration": details.get("calibration", []),
            "personas": personas,
            "backtest": ((run["summary_json"].get("details") or {}).get("forecast") or {}).get(
                "chart"
            )
            or {},
            "trend": _eval_trend(all_runs, run["mode"]),
        }
    else:
        chart = {}
    s = get_settings()
    return _page(
        request,
        "eval.html",
        p=p,
        runs=all_runs,
        run=run,
        cat=cat,
        ana=((run or {}).get("summary_json", {}).get("details") or {}).get("anomalies"),
        recur=((run or {}).get("summary_json", {}).get("details") or {}).get("recurring"),
        fcast=((run or {}).get("summary_json", {}).get("details") or {}).get("forecast"),
        simu=((run or {}).get("summary_json", {}).get("details") or {}).get("simulation"),
        advi=((run or {}).get("summary_json", {}).get("details") or {}).get("advisor"),
        cache=((run or {}).get("summary_json", {}).get("details") or {}).get("cache"),
        memo=((run or {}).get("summary_json", {}).get("details") or {}).get("memory"),
        fxd=((run or {}).get("summary_json", {}).get("details") or {}).get("fx"),
        guard=((run or {}).get("summary_json", {}).get("details") or {}).get("guardrails"),
        suites=SUITE_TABS,
        personas=personas,
        cost=cost,
        kpis=EVAL_KPIS,
        chart=chart,
        langfuse_base=f"{s.langfuse_host}/project/{s.langfuse_init_project_id}",
    )
