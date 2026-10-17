"""pipeline: pipeline_runs + agent_runs (coordinator trace), goal_snapshots; goals.kind += debt_payoff

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
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
    -- One row per coordinator run (the LangGraph thread is user:{user_id}:run:{id}).
    CREATE TABLE pipeline_runs (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      trigger             text NOT NULL CHECK (trigger IN ('manual','import','write','schedule','login','eval','resume')),
      status              text NOT NULL DEFAULT 'running' CHECK (status IN ('running','succeeded','failed')),
      degraded            boolean NOT NULL DEFAULT false,
      degraded_reason     text NULL,
      data_version_start  bigint NULL,
      data_version_end    bigint NULL,
      summary_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
      error               text NULL,
      langfuse_trace_id   text NULL,
      started_at          timestamptz NOT NULL DEFAULT now(),
      ended_at            timestamptz NULL
    );
    CREATE INDEX pipeline_runs_user_idx ON pipeline_runs (user_id, started_at DESC);
    CREATE INDEX pipeline_runs_running_idx ON pipeline_runs (started_at) WHERE status = 'running';

    -- One row per node execution (spec: agent_runs). High volume → identity bigint.
    CREATE TABLE agent_runs (
      id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      run_id             uuid NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
      step               text NOT NULL CHECK (length(step) BETWEEN 1 AND 40),
      status             text NOT NULL CHECK (status IN ('running','succeeded','skipped','failed','degraded')),
      started_at         timestamptz NOT NULL DEFAULT now(),
      ended_at           timestamptz NULL,
      summary_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
      langfuse_trace_id  text NULL
    );
    CREATE INDEX agent_runs_run_idx ON agent_runs (run_id, id);

    ALTER TABLE goals DROP CONSTRAINT goals_kind_check;
    ALTER TABLE goals ADD CONSTRAINT goals_kind_check CHECK (kind IN ('savings_target','spend_cap','debt_payoff'));

    -- Weekly goal tracker snapshots (upserted by every pipeline run in that ISO week).
    CREATE TABLE goal_snapshots (
      id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      goal_id               uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
      week_key              text NOT NULL CHECK (week_key ~ '^[0-9]{4}-W[0-9]{2}$'),
      snapshot_date         date NOT NULL,
      progress_amount       numeric(18,2) NOT NULL,
      target_amount         numeric(18,2) NOT NULL,
      progress_pct          numeric(7,2) NULL,
      projected_completion  date NULL,
      on_track              boolean NULL,
      gap                   numeric(18,2) NULL,
      status                text NOT NULL,
      currency              char(3) NOT NULL,
      data_json             jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at            timestamptz NOT NULL DEFAULT now(),
      updated_at            timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX goal_snapshots_week_uq ON goal_snapshots (goal_id, week_key);
    CREATE INDEX goal_snapshots_user_idx ON goal_snapshots (user_id, goal_id, snapshot_date DESC);
    """)
    for t in ("pipeline_runs", "agent_runs", "goal_snapshots"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS goal_snapshots, agent_runs, pipeline_runs CASCADE;
    DELETE FROM goals WHERE kind = 'debt_payoff';
    ALTER TABLE goals DROP CONSTRAINT goals_kind_check;
    ALTER TABLE goals ADD CONSTRAINT goals_kind_check CHECK (kind IN ('savings_target','spend_cap'));
    """)
