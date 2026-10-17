"""forecasting: goals (financial facts) and forecast_runs (derived, cited by reference)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
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
    -- savings_target: reach target_amount (optionally by target_date), progress = saved_amount
    --   (maintained by the user) + projected savings from the forecast.
    -- spend_cap: keep monthly spend in category_id at or below target_amount.
    CREATE TABLE goals (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind           text NOT NULL CHECK (kind IN ('savings_target','spend_cap')),
      name           text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
      target_amount  numeric(18,2) NOT NULL CHECK (target_amount > 0),
      currency       char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
      saved_amount   numeric(18,2) NOT NULL DEFAULT 0 CHECK (saved_amount >= 0),
      target_date    date NULL,
      category_id    uuid NULL REFERENCES categories(id) ON DELETE CASCADE,
      status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','achieved','archived')),
      created_at     timestamptz NOT NULL DEFAULT now(),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT goals_cap_has_category CHECK (kind <> 'spend_cap' OR category_id IS NOT NULL)
    );
    CREATE INDEX goals_user_idx ON goals (user_id) WHERE status = 'active';
    CREATE TRIGGER goals_updated_at BEFORE UPDATE ON goals
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    CREATE TRIGGER goals_dv_ins AFTER INSERT ON goals REFERENCING NEW TABLE AS new_rows
      FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();
    CREATE TRIGGER goals_dv_upd AFTER UPDATE ON goals REFERENCING NEW TABLE AS new_rows
      FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();
    CREATE TRIGGER goals_dv_del AFTER DELETE ON goals REFERENCING OLD TABLE AS old_rows
      FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();

    -- Every baseline / scenario forecast that was shown or cited. Derived: no data_version triggers.
    CREATE TABLE forecast_runs (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind                text NOT NULL CHECK (kind IN ('baseline','scenario')),
      data_version        bigint NOT NULL,
      reporting_currency  char(3) NOT NULL,
      as_of               date NOT NULL,
      horizon_days        smallint NOT NULL CHECK (horizon_days BETWEEN 7 AND 366),
      seed                bigint NOT NULL,
      n_paths             integer NOT NULL CHECK (n_paths > 0),
      low_confidence      boolean NOT NULL DEFAULT false,
      scenario_json       jsonb NULL,
      result_json         jsonb NOT NULL,
      duration_ms         integer NULL,
      created_at          timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX forecast_runs_latest_idx ON forecast_runs (user_id, kind, created_at DESC);
    """)
    for t in ("goals", "forecast_runs"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS forecast_runs, goals CASCADE;")
