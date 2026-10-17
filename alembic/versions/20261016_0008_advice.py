"""advice: recommendations, decision_trail; advisor-owned finding kinds (forecast, goal)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
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
    ALTER TABLE analysis_findings DROP CONSTRAINT analysis_findings_kind_check;
    ALTER TABLE analysis_findings ADD CONSTRAINT analysis_findings_kind_check
      CHECK (kind IN ('anomaly','pattern','subscription','fx_exposure','trend','forecast','goal'));

    -- One row per recommendation shown. Derived from findings + forecasts: no data_version triggers.
    -- `candidate_key` is stable across runs ("cancel_subscription:<series>") so a dismissed
    -- recommendation is never proposed again (with its reason passed to the advisor).
    CREATE TABLE recommendations (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      run_id              uuid NOT NULL,
      candidate_key       text NOT NULL CHECK (length(candidate_key) BETWEEN 1 AND 300),
      kind                text NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
      trigger_kind        text NOT NULL CHECK (trigger_kind IN ('anomaly','subscription','forecast_shortfall','goal_gap','pattern')),
      title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
      action              text NOT NULL CHECK (length(action) BETWEEN 1 AND 600),
      why                 text NOT NULL CHECK (length(why) <= 1200),
      est_monthly_impact  numeric(18,2) NOT NULL DEFAULT 0,
      est_total_impact    numeric(18,2) NOT NULL DEFAULT 0,
      currency            char(3) NOT NULL,
      priority            smallint NOT NULL CHECK (priority BETWEEN 1 AND 10),
      status              text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','dismissed','superseded')),
      dismiss_reason      text NULL CHECK (length(dismiss_reason) <= 500),
      feedback            text NULL CHECK (feedback IN ('up','down')),
      generated_by        text NOT NULL CHECK (generated_by IN ('llm','template')),
      data_version        bigint NOT NULL,
      created_at          timestamptz NOT NULL DEFAULT now(),
      decided_at          timestamptz NULL
    );
    CREATE INDEX recommendations_active_idx ON recommendations (user_id, priority) WHERE status = 'proposed';
    CREATE INDEX recommendations_key_idx ON recommendations (user_id, candidate_key, status);

    CREATE TABLE decision_trail (
      id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id                   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      recommendation_id         uuid NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
      trigger_kind              text NOT NULL,
      trigger_finding_ids_json  jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(trigger_finding_ids_json) = 'array'),
      evidence_txn_ids_json     jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_txn_ids_json) = 'array'),
      forecast_ref_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
      candidate_json            jsonb NOT NULL DEFAULT '{}'::jsonb,
      reasoning_text            text NOT NULL DEFAULT '',
      model                     text NULL,
      prompt_name               text NULL,
      prompt_version            integer NULL,
      grounding_report_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
      langfuse_trace_id         text NULL,
      created_at                timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX decision_trail_rec_uq ON decision_trail (recommendation_id);
    """)
    for t in ("recommendations", "decision_trail"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS decision_trail, recommendations CASCADE;
    DELETE FROM analysis_findings WHERE kind IN ('forecast','goal');
    ALTER TABLE analysis_findings DROP CONSTRAINT analysis_findings_kind_check;
    ALTER TABLE analysis_findings ADD CONSTRAINT analysis_findings_kind_check
      CHECK (kind IN ('anomaly','pattern','subscription','fx_exposure','trend'));
    """)
