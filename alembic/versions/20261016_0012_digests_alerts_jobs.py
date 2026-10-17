"""proactive delivery: digests, alerts, telegram_link_tokens, job_runs

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str) -> str:
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {table}
      USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id());
    """


def upgrade() -> None:
    op.execute("""
    -- One digest per user per period (the local date of the user's weekly slot). The UNIQUE key is
    -- the idempotency guarantee behind the 15-minute digests_due tick.
    CREATE TABLE digests (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      period_key       text NOT NULL CHECK (length(period_key) <= 20),
      channel          text NOT NULL CHECK (channel IN ('in_app','email','telegram')),
      status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','preview')),
      delivered_via    text CHECK (delivered_via IN ('in_app','smtp','telegram','log_sink')),
      subject          text NOT NULL DEFAULT '' CHECK (length(subject) <= 200),
      body_text        text NOT NULL DEFAULT '' CHECK (length(body_text) <= 8000),
      payload_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
      degraded         boolean NOT NULL DEFAULT false,
      generated_by     text CHECK (generated_by IN ('llm','template')),
      grounding_json   jsonb,
      run_id           uuid,
      error            text,
      read_at          timestamptz,
      created_at       timestamptz NOT NULL DEFAULT now(),
      delivered_at     timestamptz,
      UNIQUE (user_id, period_key)
    );
    CREATE INDEX digests_user_idx ON digests (user_id, created_at DESC);

    -- Proactive alerts (low balance ahead). The same dedupe_key is not raised again for 7 days.
    CREATE TABLE alerts (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind           text NOT NULL CHECK (kind IN ('low_balance')),
      dedupe_key     text NOT NULL CHECK (length(dedupe_key) <= 120),
      severity       text NOT NULL CHECK (severity IN ('info','warning','critical')),
      title          text NOT NULL CHECK (length(title) <= 200),
      body           text NOT NULL CHECK (length(body) <= 2000),
      payload_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
      channel        text NOT NULL CHECK (channel IN ('in_app','email','telegram')),
      delivered_via  text CHECK (delivered_via IN ('in_app','smtp','telegram','log_sink')),
      read_at        timestamptz,
      created_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX alerts_dedupe_idx ON alerts (user_id, dedupe_key, created_at DESC);
    CREATE INDEX alerts_unread_idx ON alerts (user_id) WHERE read_at IS NULL;

    -- Telegram deep-link tokens (t.me/<bot>?start=<token>): only the HMAC is stored; single use.
    CREATE TABLE telegram_link_tokens (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash  text NOT NULL UNIQUE,
      expires_at  timestamptz NOT NULL,
      used_at     timestamptz,
      created_at  timestamptz NOT NULL DEFAULT now()
    );

    -- Scheduler evidence: one row per task execution (Celery signals). System table, no user data.
    CREATE TABLE job_runs (
      id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      task         text NOT NULL,
      task_id      text,
      status       text NOT NULL CHECK (status IN ('running','succeeded','failed')),
      started_at   timestamptz NOT NULL DEFAULT now(),
      finished_at  timestamptz,
      duration_ms  integer,
      result_json  jsonb,
      error        text
    );
    CREATE INDEX job_runs_task_idx ON job_runs (task, started_at DESC);
    CREATE UNIQUE INDEX job_runs_task_id_uq ON job_runs (task_id) WHERE task_id IS NOT NULL;
    """)
    for t in ("digests", "alerts", "telegram_link_tokens"):
        op.execute(_tenant_policy(t))
    # Bot webhooks arrive without a user: binding a chat goes through this SECURITY DEFINER function
    # (the only pre-auth path), which consumes a valid token and stores the chat id.
    op.execute("""
    CREATE FUNCTION telegram_bind(p_token_hash text, p_chat_id text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
    DECLARE uid uuid;
    BEGIN
      UPDATE telegram_link_tokens SET used_at = now()
        WHERE token_hash = p_token_hash AND used_at IS NULL AND expires_at > now()
        RETURNING user_id INTO uid;
      IF uid IS NULL THEN RETURN NULL; END IF;
      UPDATE user_settings SET telegram_chat_id = p_chat_id WHERE user_id = uid;
      RETURN uid;
    END $$;
    REVOKE ALL ON FUNCTION telegram_bind(text, text) FROM PUBLIC;
    DO $grant$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_app') THEN
        GRANT EXECUTE ON FUNCTION telegram_bind(text, text) TO finpilot_app;
      END IF;
    END $grant$;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION IF EXISTS telegram_bind(text, text);
    DROP TABLE IF EXISTS job_runs, telegram_link_tokens, alerts, digests CASCADE;
    """)
