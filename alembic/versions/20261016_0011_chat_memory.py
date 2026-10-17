"""chat + memory: chat_sessions, chat_messages, memories (pgvector + lexical fallback)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
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
    -- Not financial facts: no data_version triggers (a chat or a memory never changes the numbers).
    CREATE TABLE chat_sessions (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title            text CHECK (length(title) <= 120),
      summary          text,                         -- rolling summary of turns beyond the short-term window
      started_at       timestamptz NOT NULL DEFAULT now(),
      last_message_at  timestamptz NOT NULL DEFAULT now(),
      consolidated_at  timestamptz                   -- memory writer ran up to here
    );
    CREATE INDEX chat_sessions_user_idx ON chat_sessions (user_id, last_message_at DESC);
    -- the memory_consolidation tick looks for idle, unconsolidated sessions
    CREATE INDEX chat_sessions_pending_idx ON chat_sessions (last_message_at)
      WHERE consolidated_at IS NULL OR consolidated_at < last_message_at;

    CREATE TABLE chat_messages (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      session_id         uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
      role               text NOT NULL CHECK (role IN ('user','assistant')),
      content            text NOT NULL CHECK (length(content) <= 8000),
      route              text,                       -- cache | simulator | tools | fallback | degraded
      tools_json         jsonb,                      -- tool calls made for this answer (name, args, ms, ok)
      grounding_json     jsonb,
      cache_layer        text CHECK (cache_layer IN ('miss','L2','L3','none')),
      feedback           text CHECK (feedback IN ('up','down')),
      langfuse_trace_id  text,
      latency_ms         integer CHECK (latency_ms >= 0),
      created_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX chat_messages_session_idx ON chat_messages (session_id, created_at);

    CREATE TABLE memories (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind           text NOT NULL CHECK (kind IN ('fact','preference','goal_context','decision','episode')),
      topic          text NOT NULL CHECK (length(topic) BETWEEN 1 AND 60),  -- slot: same kind+topic supersedes
      text           text NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
      embedding      vector(1536),                   -- NULL when embeddings are unavailable
      tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
      importance     numeric(3,2) NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
      confidence     numeric(3,2) NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
      pinned         boolean NOT NULL DEFAULT false,
      source         text NOT NULL CHECK (source IN ('chat','recommendation','user','eval')),
      source_ref     text,
      created_at     timestamptz NOT NULL DEFAULT now(),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      last_used_at   timestamptz,
      expires_at     timestamptz,
      superseded_by  uuid REFERENCES memories(id) ON DELETE SET NULL
    );
    CREATE INDEX memories_active_idx ON memories (user_id, kind, topic) WHERE superseded_by IS NULL;
    CREATE INDEX memories_hnsw ON memories USING hnsw (embedding vector_cosine_ops);
    CREATE INDEX memories_tsv_idx ON memories USING gin (tsv);
    CREATE INDEX memories_trgm_idx ON memories USING gin (text gin_trgm_ops);
    CREATE TRIGGER memories_updated_at BEFORE UPDATE ON memories
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)
    for t in ("chat_sessions", "chat_messages", "memories"):
        op.execute(_tenant_policy(t))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memories, chat_messages, chat_sessions CASCADE;")
