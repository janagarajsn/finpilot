"""FastAPI application entrypoint: middleware, error handling, routers, static files."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from finpilot import __version__
from finpilot.agents.advisor import AdviceError
from finpilot.agents.coordinator import PipelineError
from finpilot.agents.simulator_graph import AskError
from finpilot.api import (
    routes_advice,
    routes_answers,
    routes_auth,
    routes_categories,
    routes_chat,
    routes_dashboard,
    routes_digests,
    routes_eval,
    routes_forecast,
    routes_fx,
    routes_import,
    routes_insights,
    routes_me,
    routes_memory,
    routes_pipeline,
    routes_recurring,
    routes_transactions,
)
from finpilot.api.cookies import clear_auth_cookies, set_auth_cookies, set_csrf_cookie
from finpilot.api.deps import client_info
from finpilot.auth import service as auth
from finpilot.categories.seed import sync_system_categories
from finpilot.categories.service import CategoryError
from finpilot.config import get_settings
from finpilot.db.session import get_engine
from finpilot.forecasting.goals import GoalError
from finpilot.forecasting.inputs import ForecastError
from finpilot.ingest.import_service import ImportError_
from finpilot.ingest.sample_data import SampleDataError
from finpilot.llm.prompt_registry import get_registry
from finpilot.observability.langfuse import flush, init_langfuse
from finpilot.recurring.service import RecurringError
from finpilot.redis_client import get_redis
from finpilot.security.csrf import CSRF_COOKIE, csrf_protect, new_csrf_token
from finpilot.security.headers import security_headers
from finpilot.security.ratelimit import RateLimited
from finpilot.tenancy import ACCESS_COOKIE, REFRESH_COOKIE, principal_from_request
from finpilot.transactions.service import TxnError
from finpilot.web import views

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    init_langfuse(settings)
    get_registry()  # fail fast on broken prompt files
    try:
        sync_system_categories()
    except Exception:
        log.exception("system category sync failed (will retry next start)")
    yield
    flush()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="FinPilot",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.app_base_url.rstrip("/")],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "HX-Request",
            "HX-Target",
            "HX-Current-URL",
            "HX-Trigger",
        ],
    )

    headers = security_headers(settings.is_prod)

    @app.middleware("http")
    async def session_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 1) Transparent cookie-session refresh: expired access cookie + valid refresh cookie.
        new_pair, refresh_failed = None, False
        is_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        if (
            not is_bearer
            and REFRESH_COOKIE in request.cookies
            and principal_from_request(request) is None
        ):
            try:
                new_pair = await run_in_threadpool(
                    auth.refresh, request.cookies[REFRESH_COOKIE], client_info(request)
                )
            except auth.AuthError:
                refresh_failed = True
            except Exception:
                log.warning("cookie refresh failed", exc_info=True)
            if new_pair is not None:
                cookies = {
                    **request.cookies,
                    ACCESS_COOKIE: new_pair.access_token,
                    REFRESH_COOKIE: new_pair.refresh_token,
                }
                raw = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode("latin-1")
                request.scope["headers"] = [
                    (k, v) for k, v in request.scope["headers"] if k != b"cookie"
                ] + [(b"cookie", raw)]

        response = await call_next(request)

        if new_pair is not None:
            set_auth_cookies(response, new_pair)
        elif refresh_failed:
            clear_auth_cookies(response)
        # 2) CSRF cookie for double-submit.
        if CSRF_COOKIE not in request.cookies:
            set_csrf_cookie(response, request.state.__dict__.get("new_csrf") or new_csrf_token())
        # 3) Security headers (docs page needs its own CDN; keep CSP for everything else).
        for k, v in headers.items():
            if k == "Content-Security-Policy" and request.url.path in ("/docs", "/openapi.json"):
                continue
            response.headers.setdefault(k, v)
        # 4) Pages and API answers hold personal data: browsers must not keep a copy (so Back
        #    after sign-out cannot show one). Static assets stay cacheable.
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store, max-age=0")
            response.headers.setdefault("Pragma", "no-cache")
        return response

    @app.exception_handler(auth.AuthError)
    async def _auth_error(_: Request, exc: auth.AuthError) -> JSONResponse:
        headers = (
            {"Retry-After": str(exc.retry_after)} if isinstance(exc, auth.AccountLocked) else None
        )
        return JSONResponse(
            {"detail": str(exc), "code": exc.code}, status_code=exc.status, headers=headers
        )

    async def _domain_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=getattr(exc, "status", 400))

    for exc_type in (
        TxnError,
        CategoryError,
        RecurringError,
        ImportError_,
        SampleDataError,
        ForecastError,
        GoalError,
        AskError,
        AdviceError,
        PipelineError,
    ):
        app.add_exception_handler(exc_type, _domain_error)

    @app.exception_handler(RateLimited)
    async def _rate_limited(_: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            {"detail": "Too many requests", "code": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        wants_html = "text/html" in request.headers.get("accept", "") and not request.headers.get(
            "hx-request"
        )
        if wants_html and request.method == "GET":
            if exc.status_code == 401:
                return Response(status_code=303, headers={"Location": "/login"})
            return views.templates.TemplateResponse(
                request,
                "error.html",
                {
                    "status": exc.status_code,
                    "message": exc.detail,
                    "user": None,
                    "csrf_token": "",
                    "nav": {"main": [], "tools": []},
                    "fx": None,
                },
                status_code=exc.status_code,
            )
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["ops"])
    def ready() -> JSONResponse:
        """Readiness: dependencies reachable."""
        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as e:
            checks["postgres"] = f"error: {type(e).__name__}"
        try:
            get_redis().ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
        )

    app.include_router(routes_auth.router)
    app.include_router(routes_me.router)
    app.include_router(routes_fx.router)
    app.include_router(routes_transactions.router)
    app.include_router(routes_categories.router)
    app.include_router(routes_recurring.router)
    app.include_router(routes_import.router)
    app.include_router(routes_dashboard.router)
    app.include_router(routes_insights.router)
    app.include_router(routes_forecast.router)
    app.include_router(routes_advice.router)
    app.include_router(routes_pipeline.router)
    app.include_router(routes_answers.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_memory.router)
    app.include_router(routes_digests.router)
    app.include_router(routes_eval.router)
    app.include_router(views.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
