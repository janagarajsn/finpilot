"""evaluation tables; FX rate scope (real vs synthetic)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15

Synthetic personas (evals) use a seeded synthetic FX series so evals never touch the network.
Those rates live in `fx_rates` with scope='synthetic' and are only ever joined for users with
`is_synthetic = true`; real users only ever see scope='real'.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE fx_rates ADD COLUMN scope text NOT NULL DEFAULT 'real' CHECK (scope IN ('real','synthetic'));
    ALTER TABLE fx_rates DROP CONSTRAINT fx_rates_pkey;
    ALTER TABLE fx_rates ADD PRIMARY KEY (scope, rate_date, quote_currency);
    DROP INDEX IF EXISTS fx_rates_quote_date_idx;
    CREATE INDEX fx_rates_scope_quote_date_idx ON fx_rates (scope, quote_currency, rate_date DESC);
    ALTER TABLE fx_rates ADD CONSTRAINT fx_rates_synthetic_source
      CHECK ((scope = 'synthetic') = (source = 'synthetic'));

    ALTER TABLE transactions DROP CONSTRAINT transactions_fx_source_check;
    ALTER TABLE transactions ADD CONSTRAINT transactions_fx_source_check CHECK (
      fx_source IN ('base','mcp','fallback_api','stale_db','pinned','user_override','synthetic'));

    -- t.* is expanded when a view is created, so columns added to transactions since 0002
    -- (dedupe_key) require DROP + CREATE rather than CREATE OR REPLACE.
    DROP VIEW v_transactions_reporting;
    CREATE VIEW v_transactions_reporting WITH (security_invoker = true) AS
    SELECT
      t.*,
      u.reporting_currency,
      CASE
        WHEN t.currency = u.reporting_currency THEN t.amount
        WHEN u.reporting_currency = 'INR' THEN t.amount_inr
        ELSE round(t.amount_inr / r.rate_inr_per_unit, 2)
      END AS amount_reporting,
      CASE
        WHEN t.currency = u.reporting_currency OR u.reporting_currency = 'INR' THEN NULL
        ELSE r.rate_date
      END AS reporting_rate_date
    FROM transactions t
    JOIN users u ON u.id = t.user_id
    LEFT JOIN LATERAL (
      SELECT f.rate_inr_per_unit, f.rate_date
      FROM fx_rates f
      WHERE f.scope = CASE WHEN u.is_synthetic THEN 'synthetic' ELSE 'real' END
        AND f.quote_currency = u.reporting_currency AND f.rate_date <= t.fx_rate_date
      ORDER BY f.rate_date DESC
      LIMIT 1
    ) r ON u.reporting_currency <> 'INR';
    """)

    # ------------------------------------------------------------------ evaluation
    # Not user-owned (system quality data): readable by every signed-in user, written by
    # eval jobs. No user_id → no RLS (the RLS meta-test only covers user_id tables).
    op.execute("""
    CREATE TABLE eval_runs (
      id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      started_at             timestamptz NOT NULL DEFAULT now(),
      finished_at            timestamptz NULL,
      git_sha                text NULL,
      trigger                text NOT NULL CHECK (trigger IN ('manual','ci','scheduled')),
      mode                   text NOT NULL CHECK (mode IN ('offline','full')),
      status                 text NOT NULL DEFAULT 'running' CHECK (status IN ('running','passed','failed','error')),
      config_json            jsonb NOT NULL DEFAULT '{}'::jsonb,
      models_json            jsonb NOT NULL DEFAULT '{}'::jsonb,
      prompt_versions_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
      langfuse_run_urls_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      summary_json           jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX eval_runs_started_idx ON eval_runs (started_at DESC);

    CREATE TABLE eval_metrics (
      id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_id          uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
      suite           text NOT NULL,
      metric          text NOT NULL,
      value           double precision NOT NULL,
      unit            text NULL,
      dimension_json  jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX eval_metrics_run_idx ON eval_metrics (run_id, suite, metric);
    CREATE INDEX eval_metrics_trend_idx ON eval_metrics (suite, metric);

    CREATE TABLE eval_cases (
      id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_id             uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
      suite              text NOT NULL,
      case_id            text NOT NULL,
      input_json         jsonb NOT NULL,
      expected_json      jsonb NOT NULL,
      actual_json        jsonb NOT NULL,
      passed             boolean NOT NULL,
      score              double precision NULL,
      error_type         text NULL,
      langfuse_trace_id  text NULL
    );
    CREATE INDEX eval_cases_run_idx ON eval_cases (run_id, suite, passed);

    CREATE TABLE eval_insights (
      id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      run_id         uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
      severity       text NOT NULL CHECK (severity IN ('critical','warning','info')),
      title          text NOT NULL,
      body           text NOT NULL,
      evidence_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
      generated_by   text NOT NULL CHECK (generated_by IN ('rules','llm'))
    );
    CREATE INDEX eval_insights_run_idx ON eval_insights (run_id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS eval_insights, eval_cases, eval_metrics, eval_runs;
    ALTER TABLE transactions DROP CONSTRAINT transactions_fx_source_check;
    ALTER TABLE transactions ADD CONSTRAINT transactions_fx_source_check CHECK (
      fx_source IN ('base','mcp','fallback_api','stale_db','pinned','user_override'));
    DELETE FROM fx_rates WHERE scope = 'synthetic';
    ALTER TABLE fx_rates DROP CONSTRAINT fx_rates_synthetic_source;
    ALTER TABLE fx_rates DROP CONSTRAINT fx_rates_pkey;
    ALTER TABLE fx_rates ADD PRIMARY KEY (rate_date, quote_currency);
    ALTER TABLE fx_rates DROP COLUMN scope;
    """)
