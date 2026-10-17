"""Integration tests against real Postgres + Redis (`make up-deps`). Skipped if unreachable."""

from __future__ import annotations

import uuid

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from finpilot.config import get_settings
from finpilot.db.session import get_engine, user_session
from finpilot.llm.client import CallRecord
from tests.conftest import needs_pg, needs_redis


@needs_pg
def test_extensions_and_tables_exist():
    with get_engine().connect() as c:
        exts = {r[0] for r in c.execute(text("SELECT extname FROM pg_extension"))}
        tables = {
            r[0]
            for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        }
    assert {"vector", "pg_trgm", "citext", "pgcrypto"} <= exts
    assert {"llm_calls", "fx_rates", "fx_fetch_log"} <= tables


@needs_pg
def test_app_role_is_non_owner_without_ddl_or_bypassrls():
    with get_engine().connect() as c:
        row = c.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        assert row == ("finpilot_app", False, False)
        with pytest.raises(DBAPIError):
            c.execute(text("CREATE TABLE should_fail (id int)"))


@needs_pg
def test_readonly_role_cannot_write():
    url = get_settings().database_url.replace(
        "finpilot_app:finpilot_app", "finpilot_ro:finpilot_ro"
    )
    with create_engine(url).connect() as c, pytest.raises(DBAPIError):
        c.execute(text("INSERT INTO fx_fetch_log (route, ok) VALUES ('db', true)"))


@needs_pg
def test_set_local_user_id_does_not_leak_across_transactions():
    uid = uuid.uuid4()
    with user_session(uid) as s:
        assert s.execute(text("SELECT current_setting('app.user_id', true)")).scalar() == str(uid)
    with get_engine().connect() as c:
        assert c.execute(text("SELECT current_setting('app.user_id', true)")).scalar() in (None, "")
    with pytest.raises(ValueError), user_session("1; DROP TABLE x"):
        pass


@needs_pg
def test_llm_call_sink_persists_rows():
    from finpilot.db.llm_log import db_sink

    marker = f"test-{uuid.uuid4().hex[:8]}"
    db_sink(
        CallRecord(
            agent=marker,
            model="openai/gpt-4o-mini",
            kind="json",
            prompt_name="hello",
            prompt_version=1,
            user_id=str(uuid.uuid4()),
            prompt_tokens=10,
            cached_tokens=4,
            completion_tokens=3,
            latency_ms=12,
            cost_usd=0.0001,
        )
    )
    with create_engine(get_settings().database_admin_url).connect() as c:
        row = c.execute(
            text("SELECT cached_tokens, kind FROM llm_calls WHERE agent=:a"), {"a": marker}
        ).one()
    assert row == (4, "json")


@needs_pg
@needs_redis
def test_health_and_ready_endpoints():
    from finpilot.api.main import app

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        r = client.get("/ready")
        assert r.status_code == 200 and r.json()["checks"] == {"postgres": "ok", "redis": "ok"}


@needs_redis
def test_redis_supports_json_and_search_for_langgraph_checkpointer():
    r = redis.Redis.from_url(get_settings().redis_url)
    modules = {m[b"name"].decode() if isinstance(m, dict) else str(m) for m in r.module_list()}
    info = r.info("server")
    assert int(str(info["redis_version"]).split(".")[0]) >= 8 or {"ReJSON", "search"} <= modules
    r.json().set("test:json", "$", {"a": 1})
    assert r.json().get("test:json") == {"a": 1}
    r.delete("test:json")
