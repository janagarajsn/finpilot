"""categories, merchant memory, recurring rules, imports, data_version triggers

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _tenant_policy(table: str) -> str:
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {table}
      USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id());
    """


def upgrade() -> None:
    # ------------------------------------------------------------------ categories
    op.execute(f"""
    CREATE TABLE categories (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id        uuid NULL REFERENCES users(id) ON DELETE CASCADE,   -- NULL = system
      slug           text NULL,                                            -- stable id for system rows
      name           text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 60),
      flow_type      text NOT NULL CHECK (flow_type IN ('income','expense','transfer','savings')),
      nature         text NOT NULL CHECK (nature IN ('fixed','essential_variable','discretionary','n/a')),
      parent_id      uuid NULL REFERENCES categories(id) ON DELETE SET NULL,
      color          text NOT NULL DEFAULT '#94a3b8' CHECK (color ~ '^#[0-9a-fA-F]{{6}}$'),
      icon           text NULL CHECK (length(icon) <= 16),
      description    text NOT NULL DEFAULT '' CHECK (length(description) <= 500),
      keywords_json  jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(keywords_json) = 'array'),
      examples_json  jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(examples_json) = 'array'),
      is_system      boolean NOT NULL DEFAULT false,
      archived       boolean NOT NULL DEFAULT false,
      created_at     timestamptz NOT NULL DEFAULT now(),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT categories_system_has_no_owner CHECK (is_system = (user_id IS NULL)),
      CONSTRAINT categories_system_has_slug CHECK (NOT is_system OR slug IS NOT NULL),
      CONSTRAINT categories_income_nature CHECK (flow_type = 'expense' OR nature = 'n/a')
    );
    CREATE UNIQUE INDEX categories_owner_name_uq
      ON categories (coalesce(user_id, '{ZERO_UUID}'::uuid), lower(btrim(name)));
    CREATE UNIQUE INDEX categories_system_slug_uq ON categories (slug) WHERE user_id IS NULL;
    CREATE INDEX categories_user_idx ON categories (user_id);
    CREATE TRIGGER categories_updated_at BEFORE UPDATE ON categories
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- System rows are readable by everyone; only own rows are writable.
    ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
    CREATE POLICY categories_read ON categories FOR SELECT
      USING (user_id IS NULL OR user_id = app_current_user_id());
    CREATE POLICY categories_insert ON categories FOR INSERT WITH CHECK (user_id = app_current_user_id());
    CREATE POLICY categories_update ON categories FOR UPDATE
      USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id());
    CREATE POLICY categories_delete ON categories FOR DELETE USING (user_id = app_current_user_id());

    CREATE TABLE hidden_categories (
      user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      category_id  uuid NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
      created_at   timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (user_id, category_id)
    );

    CREATE TABLE merchant_memory (
      user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      merchant_norm  text NOT NULL CHECK (length(merchant_norm) BETWEEN 1 AND 200),
      category_id    uuid NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
      source         text NOT NULL CHECK (source IN ('user','llm','rule','import')),
      hits           integer NOT NULL DEFAULT 1 CHECK (hits >= 0),
      updated_at     timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (user_id, merchant_norm)
    );
    """)

    # ------------------------------------------------------------------ recurring rules
    op.execute("""
    CREATE TABLE recurring_rules (
      id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      kind                    text NOT NULL CHECK (kind IN ('expense','income')),
      description             text NOT NULL CHECK (length(btrim(description)) BETWEEN 1 AND 200),
      amount                  numeric(18,2) NOT NULL CHECK (amount > 0),
      currency                char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
      category_id             uuid NULL REFERENCES categories(id) ON DELETE SET NULL,
      account_id              uuid NULL REFERENCES accounts(id) ON DELETE SET NULL,
      frequency               text NOT NULL
                                CHECK (frequency IN ('daily','weekly','biweekly','monthly','quarterly','yearly')),
      interval                smallint NOT NULL DEFAULT 1 CHECK (interval BETWEEN 1 AND 52),
      day_of_month            smallint NULL CHECK (day_of_month BETWEEN 1 AND 31),
      weekday                 smallint NULL CHECK (weekday BETWEEN 0 AND 6),     -- 0 = Monday
      start_date              date NOT NULL,
      end_date                date NULL,
      next_run_date           date NULL,
      status                  text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','ended')),
      last_materialized_date  date NULL,
      skip_dates_json         jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(skip_dates_json) = 'array'),
      notes                   text NULL CHECK (length(notes) <= 1000),
      created_at              timestamptz NOT NULL DEFAULT now(),
      updated_at              timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT recurring_end_after_start CHECK (end_date IS NULL OR end_date >= start_date)
    );
    CREATE INDEX recurring_rules_user_idx ON recurring_rules (user_id);
    CREATE INDEX recurring_rules_due_idx ON recurring_rules (next_run_date) WHERE status = 'active';
    CREATE TRIGGER recurring_rules_updated_at BEFORE UPDATE ON recurring_rules
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)

    # ------------------------------------------------------------------ imports
    op.execute("""
    CREATE TABLE import_batches (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      filename         text NOT NULL CHECK (length(filename) <= 255),
      file_kind        text NOT NULL CHECK (file_kind IN ('csv','xlsx')),
      file_sha256      text NOT NULL,
      status           text NOT NULL DEFAULT 'uploaded'
                         CHECK (status IN ('uploaded','importing','imported','failed','undone')),
      mapping_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
      options_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
      row_counts_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
      errors_json      jsonb NOT NULL DEFAULT '[]'::jsonb,
      created_at       timestamptz NOT NULL DEFAULT now(),
      imported_at      timestamptz NULL,
      undone_at        timestamptz NULL
    );
    CREATE INDEX import_batches_user_created_idx ON import_batches (user_id, created_at DESC);

    -- Raw upload kept only until the batch is imported/undone (then deleted).
    CREATE TABLE import_files (
      batch_id  uuid PRIMARY KEY REFERENCES import_batches(id) ON DELETE CASCADE,
      user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      content   bytea NOT NULL CHECK (octet_length(content) <= 5242880)
    );
    """)

    # ------------------------------------------------------------------ transactions: FKs + dedupe
    op.execute("""
    ALTER TABLE transactions
      ADD COLUMN dedupe_key text NULL,
      ADD CONSTRAINT transactions_category_fk FOREIGN KEY (category_id)
        REFERENCES categories(id) ON DELETE RESTRICT,
      ADD CONSTRAINT transactions_recurring_fk FOREIGN KEY (recurring_rule_id)
        REFERENCES recurring_rules(id) ON DELETE SET NULL,
      ADD CONSTRAINT transactions_import_fk FOREIGN KEY (import_batch_id)
        REFERENCES import_batches(id) ON DELETE CASCADE;
    CREATE INDEX transactions_user_dedupe_idx ON transactions (user_id, dedupe_key);
    CREATE INDEX transactions_import_idx ON transactions (import_batch_id) WHERE import_batch_id IS NOT NULL;
    -- A rule materializes at most one transaction per date (idempotent ticks / catch-up).
    CREATE UNIQUE INDEX transactions_rule_date_uq ON transactions (recurring_rule_id, date)
      WHERE recurring_rule_id IS NOT NULL;
    CREATE INDEX transactions_desc_trgm_idx ON transactions USING gin (raw_description gin_trgm_ops);
    """)

    for t in ("hidden_categories", "merchant_memory", "recurring_rules", "import_batches", "import_files"):
        op.execute(_tenant_policy(t))

    # ------------------------------------------------------------------ data_version
    # Any write that changes financial facts bumps users.data_version in the SAME transaction.
    # Statement-level triggers with transition tables: a 50k-row import bumps once per user.
    op.execute("""
    CREATE FUNCTION bump_data_version_rows() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        UPDATE users SET data_version = data_version + 1
         WHERE id IN (SELECT DISTINCT user_id FROM old_rows WHERE user_id IS NOT NULL);
      ELSE
        UPDATE users SET data_version = data_version + 1
         WHERE id IN (SELECT DISTINCT user_id FROM new_rows WHERE user_id IS NOT NULL);
      END IF;
      RETURN NULL;
    END $$;

    CREATE FUNCTION bump_data_version_on_currency() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.reporting_currency IS DISTINCT FROM OLD.reporting_currency THEN
        NEW.data_version := OLD.data_version + 1;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER users_currency_dv BEFORE UPDATE OF reporting_currency ON users
      FOR EACH ROW EXECUTE FUNCTION bump_data_version_on_currency();
    """)
    for t in ("transactions", "categories", "recurring_rules", "accounts"):
        op.execute(f"""
        CREATE TRIGGER {t}_dv_ins AFTER INSERT ON {t} REFERENCING NEW TABLE AS new_rows
          FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();
        CREATE TRIGGER {t}_dv_upd AFTER UPDATE ON {t} REFERENCING NEW TABLE AS new_rows
          FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();
        CREATE TRIGGER {t}_dv_del AFTER DELETE ON {t} REFERENCING OLD TABLE AS old_rows
          FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_rows();
        """)


def downgrade() -> None:
    for t in ("transactions", "categories", "recurring_rules", "accounts"):
        op.execute(f"DROP TRIGGER IF EXISTS {t}_dv_ins ON {t}; DROP TRIGGER IF EXISTS {t}_dv_upd ON {t}; "
                   f"DROP TRIGGER IF EXISTS {t}_dv_del ON {t};")
    op.execute("""
    DROP TRIGGER IF EXISTS users_currency_dv ON users;
    DROP FUNCTION IF EXISTS bump_data_version_rows(), bump_data_version_on_currency();
    DROP INDEX IF EXISTS transactions_rule_date_uq, transactions_import_idx, transactions_user_dedupe_idx,
      transactions_desc_trgm_idx;
    ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_category_fk,
      DROP CONSTRAINT IF EXISTS transactions_recurring_fk, DROP CONSTRAINT IF EXISTS transactions_import_fk,
      DROP COLUMN IF EXISTS dedupe_key;
    DROP TABLE IF EXISTS import_files, import_batches, recurring_rules, merchant_memory,
      hidden_categories, categories CASCADE;
    """)
