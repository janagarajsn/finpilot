"""baseline: extensions, privileges, llm_calls, fx_rates, fx_fetch_log

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for ext in ("pgcrypto", "citext", "vector", "pg_trgm"):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    # App role (non-owner) gets DML only; read-only dev role gets SELECT. Roles are created by
    # deploy/postgres/init; guard so migrations also work on a bare database (CI).
    op.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_app') THEN
        GRANT USAGE ON SCHEMA public TO finpilot_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finpilot_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO finpilot_app;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_admin') THEN
        GRANT USAGE ON SCHEMA public TO finpilot_admin;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finpilot_admin;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO finpilot_admin;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_ro') THEN
        GRANT USAGE ON SCHEMA public TO finpilot_ro;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO finpilot_ro;
      END IF;
    END $$;
    """)

    op.execute("""
    CREATE TABLE llm_calls (
      id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id           uuid NULL,
      agent             text NOT NULL,
      kind              text NOT NULL CHECK (kind IN ('chat','json','tools','embed')),
      model             text NOT NULL,
      prompt_name       text NULL,
      prompt_version    integer NULL,
      prompt_tokens     integer NOT NULL DEFAULT 0,
      cached_tokens     integer NOT NULL DEFAULT 0,
      completion_tokens integer NOT NULL DEFAULT 0,
      latency_ms        integer NOT NULL DEFAULT 0,
      cost_usd          numeric(12,8) NOT NULL DEFAULT 0,
      cache_layer       text NULL CHECK (cache_layer IN ('L0','L1','L2','L3')),
      ok                boolean NOT NULL DEFAULT true,
      error             text NULL,
      created_at        timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX llm_calls_user_created_idx ON llm_calls (user_id, created_at DESC);
    CREATE INDEX llm_calls_agent_created_idx ON llm_calls (agent, created_at DESC);
    """)

    op.execute("""
    CREATE TABLE fx_rates (
      rate_date          date NOT NULL,
      quote_currency     char(3) NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
      rate_inr_per_unit  numeric(18,8) NOT NULL CHECK (rate_inr_per_unit > 0),
      source             text NOT NULL CHECK (source IN ('mcp','fallback_api','pinned','synthetic')),
      fetched_at         timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (rate_date, quote_currency)
    );
    CREATE INDEX fx_rates_quote_date_idx ON fx_rates (quote_currency, rate_date DESC);

    CREATE TABLE fx_fetch_log (
      id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      requested_at  timestamptz NOT NULL DEFAULT now(),
      route         text NOT NULL CHECK (route IN ('mcp','http','db','pinned')),
      ok            boolean NOT NULL,
      latency_ms    integer NOT NULL DEFAULT 0,
      error         text NULL
    );
    CREATE INDEX fx_fetch_log_requested_idx ON fx_fetch_log (requested_at DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fx_fetch_log, fx_rates, llm_calls")
