"""analysis: detected recurrences, analyzer findings, insight snapshots

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15

None of these tables carry data_version triggers: they are DERIVED from financial facts (the
analyzer rewrites them on every run), so writing them must not invalidate caches. User actions
on them (dismiss, usage rating) clear the user's insight snapshot instead.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERIODS = "'weekly','biweekly','monthly','quarterly','yearly'"


def _tenant_policy(table: str) -> str:
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {table}
      USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id());
    """


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE detected_recurrences (
      id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      series_key           text NOT NULL CHECK (length(series_key) BETWEEN 1 AND 300),
      merchant_norm        text NOT NULL CHECK (length(merchant_norm) BETWEEN 1 AND 200),
      description          text NOT NULL CHECK (length(description) <= 500),
      kind                 text NOT NULL CHECK (kind IN ('expense','income')),
      currency             char(3) NOT NULL CHECK (currency ~ '^[A-Z]{{3}}$'),
      category_id          uuid NULL REFERENCES categories(id) ON DELETE SET NULL,
      period               text NOT NULL CHECK (period IN ({PERIODS})),
      period_days          numeric(8,2) NOT NULL CHECK (period_days > 0),
      amount_mean          numeric(18,2) NOT NULL CHECK (amount_mean > 0),
      amount_typical       numeric(18,2) NOT NULL CHECK (amount_typical > 0),   -- median of the last 3
      amount_last          numeric(18,2) NOT NULL CHECK (amount_last > 0),
      amount_cv            numeric(8,4) NOT NULL CHECK (amount_cv >= 0),
      n_occurrences        integer NOT NULL CHECK (n_occurrences >= 2),
      first_seen           date NOT NULL,
      last_seen            date NOT NULL,
      next_expected        date NOT NULL,
      status               text NOT NULL DEFAULT 'active' CHECK (status IN ('active','lapsed','gone')),
      confidence           numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
      price_history_json   jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(price_history_json) = 'array'),
      txn_ids_json         jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(txn_ids_json) = 'array'),
      usage_rating         smallint NULL CHECK (usage_rating BETWEEN 0 AND 5),
      usage_rated_at       timestamptz NULL,
      promoted_to_rule_id  uuid NULL REFERENCES recurring_rules(id) ON DELETE SET NULL,
      dismissed            boolean NOT NULL DEFAULT false,
      dismissed_at         timestamptz NULL,
      last_detected_at     timestamptz NOT NULL DEFAULT now(),
      created_at           timestamptz NOT NULL DEFAULT now(),
      updated_at           timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT detected_dates_ordered CHECK (first_seen <= last_seen AND last_seen < next_expected)
    );
    CREATE UNIQUE INDEX detected_recurrences_series_uq ON detected_recurrences (user_id, series_key);
    CREATE INDEX detected_recurrences_open_idx ON detected_recurrences (user_id)
      WHERE status <> 'gone' AND NOT dismissed;
    CREATE TRIGGER detected_recurrences_updated_at BEFORE UPDATE ON detected_recurrences
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    ALTER TABLE transactions ADD CONSTRAINT transactions_detected_fk FOREIGN KEY (detected_recurrence_id)
      REFERENCES detected_recurrences(id) ON DELETE SET NULL;

    -- Analyzer findings. `fingerprint` is stable across runs (type + subject + evidence), so a
    -- finding keeps its id while it stays true; findings that stop being true become inactive
    -- (never deleted: recommendations and the decision trail cite finding ids).
    CREATE TABLE analysis_findings (
      id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      fingerprint       text NOT NULL CHECK (length(fingerprint) BETWEEN 1 AND 300),
      kind              text NOT NULL CHECK (kind IN ('anomaly','pattern','subscription','fx_exposure','trend')),
      type              text NOT NULL CHECK (length(type) BETWEEN 1 AND 60),
      severity          text NOT NULL CHECK (severity IN ('info','low','medium','high')),
      title             text NOT NULL CHECK (length(title) <= 300),
      body              text NOT NULL CHECK (length(body) <= 2000),
      metric            text NULL,
      baseline          numeric(20,4) NULL,
      observed          numeric(20,4) NULL,
      threshold         numeric(20,4) NULL,
      unit              text NULL,
      currency          char(3) NULL,
      evidence_txn_ids  jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_txn_ids) = 'array'),
      data_json         jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(data_json) = 'object'),
      period_start      date NULL,
      period_end        date NULL,
      as_of             date NOT NULL,
      active            boolean NOT NULL DEFAULT true,
      dismissed_at      timestamptz NULL,
      first_seen_at     timestamptz NOT NULL DEFAULT now(),
      last_seen_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX analysis_findings_fp_uq ON analysis_findings (user_id, fingerprint);
    CREATE INDEX analysis_findings_active_idx ON analysis_findings (user_id, kind) WHERE active;

    -- Precomputed per-user payloads (analysis report now; dashboard/forecast later). Valid while
    -- data_version, reporting_currency and as_of match.
    CREATE TABLE insight_snapshots (
      user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind                text NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
      data_version        bigint NOT NULL,
      reporting_currency  char(3) NOT NULL,
      as_of               date NOT NULL,
      payload_json        jsonb NOT NULL,
      duration_ms         integer NULL,
      created_at          timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (user_id, kind)
    );
    """)
    for t in ("detected_recurrences", "analysis_findings", "insight_snapshots"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("""
    ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_detected_fk;
    DROP TABLE IF EXISTS insight_snapshots, analysis_findings, detected_recurrences CASCADE;
    """)
