"""Shared fixtures. Tests never hit Mesh or the FX source unless marked `live`.

DB/Redis tests run against a throwaway database `finpilot_test` (re-created per session from
the migrations) and Redis DB 15, so the developer's dev data is never touched.
"""

from __future__ import annotations

import os

# Hermetic defaults BEFORE any finpilot import reads settings.
os.environ["APP_ENV"] = "test"
os.environ["FINPILOT_ENV_FILE"] = ""  # never read the developer's real .env (keys!) in tests
for var in (
    "MESH_API_KEY",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "EMBEDDINGS_ENABLED",
):
    os.environ.pop(var, None)
os.environ.setdefault("FX_MCP_URL", "http://127.0.0.1:9/mcp")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")

_PG_HOST = os.getenv("TEST_PG_HOST", "localhost:5432")
_TEST_DB = "finpilot_test"
_OWNER = os.getenv("TEST_PG_OWNER", "finpilot:finpilot")
os.environ["DATABASE_URL"] = f"postgresql+psycopg://finpilot_app:finpilot_app@{_PG_HOST}/{_TEST_DB}"
os.environ["DATABASE_MIGRATION_URL"] = f"postgresql+psycopg://{_OWNER}@{_PG_HOST}/{_TEST_DB}"
os.environ["DATABASE_ADMIN_URL"] = (
    f"postgresql+psycopg://finpilot_admin:finpilot_admin@{_PG_HOST}/{_TEST_DB}"
)
_REDIS = os.getenv("TEST_REDIS_HOST", "localhost:6379")
os.environ["REDIS_URL"] = f"redis://{_REDIS}/15"
os.environ["LANGGRAPH_KEY_PREFIX"] = (
    "lgtest"  # checkpoints use DB 0 (Redis Search); separate prefix + index
)
os.environ["CELERY_EAGER"] = "true"

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import redis as redis_lib  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from finpilot.config import get_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pg_reachable() -> bool:
    try:
        eng = create_engine(f"postgresql+psycopg://{_OWNER}@{_PG_HOST}/postgres")
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _redis_reachable() -> bool:
    try:
        return bool(redis_lib.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1).ping())
    except Exception:
        return False


PG_OK = _pg_reachable()
REDIS_OK = _redis_reachable()
needs_pg = pytest.mark.skipif(not PG_OK, reason="Postgres not reachable (make up-deps)")
needs_redis = pytest.mark.skipif(not REDIS_OK, reason="Redis not reachable (make up-deps)")


def _recreate_test_db() -> None:
    admin = create_engine(
        f"postgresql+psycopg://{_OWNER}@{_PG_HOST}/postgres", isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as c:
        c.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": _TEST_DB},
        )
        c.execute(text(f'DROP DATABASE IF EXISTS "{_TEST_DB}"'))
        c.execute(text(f'CREATE DATABASE "{_TEST_DB}"'))
        for role in ("finpilot_app", "finpilot_admin", "finpilot_ro"):
            exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
            if exists.first():
                c.execute(text(f'GRANT CONNECT ON DATABASE "{_TEST_DB}" TO {role}'))
    admin.dispose()
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_MIGRATION_URL"])
    command.upgrade(cfg, "head")
    from finpilot.categories.seed import sync_system_categories

    sync_system_categories(os.environ["DATABASE_ADMIN_URL"])


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    if PG_OK:
        get_settings.cache_clear()
        _recreate_test_db()
    yield


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def redis_client():
    if not REDIS_OK:
        pytest.skip("Redis not reachable")
    r = redis_lib.Redis.from_url(os.environ["REDIS_URL"])
    r.flushdb()
    yield r
    r.flushdb()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def python_files(root: Path = REPO_ROOT) -> list[Path]:
    skip = {
        ".venv",
        ".git",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
    }
    return [p for p in root.rglob("*.py") if not (set(p.relative_to(root).parts) & skip)]
