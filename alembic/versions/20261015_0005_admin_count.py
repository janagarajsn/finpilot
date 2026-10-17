"""first-admin rule: the first real user to register while no admin exists becomes admin

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION auth_admin_count() RETURNS bigint
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
      AS $$ SELECT count(*) FROM users WHERE role = 'admin' AND NOT is_synthetic $$;
    REVOKE ALL ON FUNCTION auth_admin_count() FROM PUBLIC;
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_app') THEN
        GRANT EXECUTE ON FUNCTION auth_admin_count() TO finpilot_app;
      END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_admin_count()")
