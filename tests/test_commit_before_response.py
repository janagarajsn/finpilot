"""Writes must be committed BEFORE the response is sent (read-your-writes for clients).

FastAPI's default for yield-dependencies ends them after the response; our get_db commits in its
teardown, so every route must declare it with scope="function". A live check found 1/40 stale
reads before this was enforced.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from finpilot.api.main import app
from finpilot.tenancy import get_db


def _walk(dep):
    yield dep
    for sub in dep.dependencies:
        yield from _walk(sub)


def api_routes(routes) -> list[APIRoute]:
    """All APIRoutes, including those inside lazily included routers (FastAPI >= 0.140)."""
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif getattr(r, "original_router", None) is not None:
            out += api_routes(r.original_router.routes)
    return out


def test_every_get_db_dependency_commits_before_response():
    offenders, checked = [], 0
    for route in api_routes(app.router.routes):
        for d in _walk(route.dependant):
            if d.call is get_db:
                checked += 1
            if d.call is get_db and getattr(d, "scope", None) != "function":
                offenders.append(f"{sorted(route.methods)} {route.path}")
    assert checked >= 30, f"only {checked} get_db usages found: route discovery is broken"
    assert not offenders, offenders
