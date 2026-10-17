"""answers: semantic_cache (L3, pgvector) and answer_log (cache layer + latency per answer)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
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
    -- L3: answers to free-form phrasings, reusable for a CLOSE phrasing by the SAME user at the SAME
    -- data_version (+ intent, period, currency, date bucket). Never shared across users (RLS + filter).
    CREATE TABLE semantic_cache (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      intent              text NOT NULL,
      period              text NOT NULL,
      question_text       text NOT NULL CHECK (length(question_text) <= 500),
      embedding           vector(1536) NOT NULL,
      answer_json         jsonb NOT NULL,
      data_version        bigint NOT NULL,
      reporting_currency  char(3) NOT NULL,
      as_of_bucket        text NOT NULL,
      created_at          timestamptz NOT NULL DEFAULT now(),
      hits                integer NOT NULL DEFAULT 0
    );
    CREATE INDEX semantic_cache_lookup_idx ON semantic_cache (user_id, intent, period, data_version);
    CREATE INDEX semantic_cache_hnsw ON semantic_cache USING hnsw (embedding vector_cosine_ops);

    -- One row per answered question (instant chips, ask box, chat later): which cache layer served
    -- it and how fast. Feeds the hit-rate / latency panels. High volume → identity bigint.
    CREATE TABLE answer_log (
      id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      intent        text NOT NULL,
      period        text NOT NULL,
      route         text NOT NULL,          -- how the intent was found: chip | rules | embedding | llm | default
      cache_layer   text NOT NULL CHECK (cache_layer IN ('miss','L2','L3','none')),
      generated_by  text NOT NULL,          -- template | llm | simulator | cache
      latency_ms    integer NOT NULL CHECK (latency_ms >= 0),
      degraded      boolean NOT NULL DEFAULT false,
      created_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX answer_log_user_idx ON answer_log (user_id, created_at DESC);
    """)
    for t in ("semantic_cache", "answer_log"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS answer_log, semantic_cache CASCADE;")
