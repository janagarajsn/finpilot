"""identity, tenancy (RLS), accounts, transactions core, reporting view

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15

RLS model: every user-owned table has `user_id` and a policy comparing it with
`app_current_user_id()` (from `SET LOCAL app.user_id`). The runtime role `finpilot_app` is not
the owner and has NOBYPASSRLS, so policies always apply. Pre-authentication lookups that
cannot know the user yet (email → user, refresh/reset token hash → owner) go through narrow
SECURITY DEFINER functions instead of a bypass role.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that hold per-user rows; each gets the standard tenant policy.
USER_TABLES = (
    "refresh_tokens",
    "password_reset_tokens",
    "auth_events",
    "user_settings",
    "accounts",
    "transactions",
    "llm_calls",
)


def _tenant_policy(table: str, column: str = "user_id") -> str:
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {table}
      USING ({column} = app_current_user_id())
      WITH CHECK ({column} = app_current_user_id());
    """


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION app_current_user_id() RETURNS uuid
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

    CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
    """)

    # ------------------------------------------------------------------ users
    op.execute("""
    CREATE TABLE users (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      email               citext NOT NULL UNIQUE CHECK (length(email) <= 254 AND email ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'),
      password_hash       text NOT NULL,
      name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
      reporting_currency  char(3) NOT NULL DEFAULT 'INR' CHECK (reporting_currency ~ '^[A-Z]{3}$'),
      timezone            text NOT NULL DEFAULT 'Asia/Kolkata',
      role                text NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
      data_version        bigint NOT NULL DEFAULT 0,
      is_synthetic        boolean NOT NULL DEFAULT false,
      onboarded_at        timestamptz NULL,
      email_verified_at   timestamptz NULL,
      failed_login_count  integer NOT NULL DEFAULT 0,
      locked_until        timestamptz NULL,
      password_changed_at timestamptz NOT NULL DEFAULT now(),
      created_at          timestamptz NOT NULL DEFAULT now(),
      updated_at          timestamptz NOT NULL DEFAULT now()
    );
    CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    ALTER TABLE users ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON users
      USING (id = app_current_user_id()) WITH CHECK (id = app_current_user_id());
    """)

    # ------------------------------------------------------------------ auth
    op.execute("""
    CREATE TABLE refresh_tokens (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      family_id   uuid NOT NULL,
      token_hash  text NOT NULL UNIQUE,
      parent_id   uuid NULL REFERENCES refresh_tokens(id) ON DELETE SET NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      expires_at  timestamptz NOT NULL,
      used_at     timestamptz NULL,
      revoked_at  timestamptz NULL,
      user_agent  text NULL,
      ip          inet NULL
    );
    CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id);
    CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id);

    CREATE TABLE password_reset_tokens (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash  text NOT NULL UNIQUE,
      created_at  timestamptz NOT NULL DEFAULT now(),
      expires_at  timestamptz NOT NULL,
      used_at     timestamptz NULL
    );
    CREATE INDEX password_reset_tokens_user_idx ON password_reset_tokens (user_id);

    CREATE TABLE auth_events (
      id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id     uuid NULL REFERENCES users(id) ON DELETE CASCADE,
      kind        text NOT NULL CHECK (kind IN (
                    'register','login_ok','login_failed','locked','logout','refresh',
                    'refresh_reuse','password_changed','password_reset_requested',
                    'password_reset_done','profile_updated','account_deleted')),
      ip          inet NULL,
      user_agent  text NULL,
      detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX auth_events_user_created_idx ON auth_events (user_id, created_at DESC);
    """)

    # ------------------------------------------------------------------ settings & accounts
    op.execute("""
    CREATE TABLE user_settings (
      user_id             uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      digest_enabled      boolean NOT NULL DEFAULT true,
      digest_channel      text NOT NULL DEFAULT 'in_app' CHECK (digest_channel IN ('in_app','email','telegram')),
      digest_dow          smallint NOT NULL DEFAULT 0 CHECK (digest_dow BETWEEN 0 AND 6),  -- 0 = Sunday
      digest_hour         smallint NOT NULL DEFAULT 9 CHECK (digest_hour BETWEEN 0 AND 23),
      risk_alert_enabled  boolean NOT NULL DEFAULT true,
      cash_buffer         numeric(18,2) NULL CHECK (cash_buffer IS NULL OR cash_buffer >= 0),
      telegram_chat_id    text NULL,
      created_at          timestamptz NOT NULL DEFAULT now(),
      updated_at          timestamptz NOT NULL DEFAULT now()
    );
    CREATE TRIGGER user_settings_updated_at BEFORE UPDATE ON user_settings
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE accounts (
      id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name                  text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
      type                  text NOT NULL DEFAULT 'bank'
                              CHECK (type IN ('bank','credit_card','cash','wallet','investment','loan')),
      currency              char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
      opening_balance       numeric(18,2) NOT NULL DEFAULT 0,
      opening_balance_date  date NULL,
      archived              boolean NOT NULL DEFAULT false,
      created_at            timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX accounts_user_name_uq ON accounts (user_id, lower(name));
    """)

    # ------------------------------------------------------------------ transactions (core)
    # FKs to categories / recurring / imports are added by the migrations that create them.
    op.execute("""
    CREATE TABLE transactions (
      id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      account_id              uuid NULL REFERENCES accounts(id) ON DELETE SET NULL,
      date                    date NOT NULL,
      raw_description         text NOT NULL CHECK (length(raw_description) <= 500),
      merchant_norm           text NULL,
      amount                  numeric(18,2) NOT NULL CHECK (amount <> 0),       -- original, signed (negative = expense)
      currency                char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'), -- original
      fx_rate                 numeric(18,8) NOT NULL CHECK (fx_rate > 0),       -- original → INR
      amount_inr              numeric(18,2) NOT NULL,
      fx_rate_date            date NOT NULL,
      fx_source               text NOT NULL
                                CHECK (fx_source IN ('base','mcp','fallback_api','stale_db','pinned','user_override')),
      category_id             uuid NULL,
      cat_source              text NULL CHECK (cat_source IN ('rule','memory','llm','user','import')),
      cat_confidence          numeric(4,3) NULL CHECK (cat_confidence BETWEEN 0 AND 1),
      cat_rationale           text NULL,
      needs_review            boolean NOT NULL DEFAULT false,
      notes                   text NULL CHECK (length(notes) <= 1000),
      source                  text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','import','recurring_rule')),
      recurring_rule_id       uuid NULL,
      detected_recurrence_id  uuid NULL,
      import_batch_id         uuid NULL,
      hash                    text NOT NULL,
      created_at              timestamptz NOT NULL DEFAULT now(),
      updated_at              timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT transactions_inr_is_base CHECK (
        currency <> 'INR' OR (fx_rate = 1 AND amount_inr = amount AND fx_source IN ('base','user_override'))
      ),
      CONSTRAINT transactions_sign_consistent CHECK (sign(amount_inr) = sign(amount))
    );
    CREATE UNIQUE INDEX transactions_user_hash_uq ON transactions (user_id, hash);
    CREATE INDEX transactions_user_date_idx ON transactions (user_id, date DESC);
    CREATE INDEX transactions_user_category_idx ON transactions (user_id, category_id);
    CREATE INDEX transactions_user_merchant_idx ON transactions (user_id, merchant_norm);
    CREATE INDEX transactions_review_idx ON transactions (user_id) WHERE needs_review;
    CREATE TRIGGER transactions_updated_at BEFORE UPDATE ON transactions
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)

    for t in USER_TABLES:
        if t == "auth_events":
            # failed logins for unknown emails are recorded with user_id NULL
            op.execute("""
            ALTER TABLE auth_events ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_read ON auth_events FOR SELECT USING (user_id = app_current_user_id());
            CREATE POLICY tenant_insert ON auth_events FOR INSERT
              WITH CHECK (user_id IS NULL OR user_id = app_current_user_id());
            """)
        elif t == "llm_calls":
            op.execute("""
            ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_read ON llm_calls FOR SELECT USING (user_id = app_current_user_id());
            """)
        else:
            op.execute(_tenant_policy(t))

    # ------------------------------------------------------------------ reporting view
    # amount_reporting: original amount when it is already in the reporting currency (exact
    # round-trips), INR when reporting in INR, else amount_inr / (INR per unit of the reporting
    # currency) at the transaction's rate date (nearest previous stored rate).
    # security_invoker → RLS of the querying role applies to the underlying tables.
    op.execute("""
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
      WHERE f.quote_currency = u.reporting_currency AND f.rate_date <= t.fx_rate_date
      ORDER BY f.rate_date DESC
      LIMIT 1
    ) r ON u.reporting_currency <> 'INR';
    """)

    # ------------------------------------------------------------------ pre-auth lookups
    # SECURITY DEFINER (owner bypasses RLS). Each returns only what the auth flow needs.
    op.execute("""
    CREATE FUNCTION auth_lookup_user(p_email citext)
      RETURNS TABLE (id uuid, password_hash text, role text, failed_login_count integer,
                     locked_until timestamptz)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
      AS $$ SELECT id, password_hash, role, failed_login_count, locked_until
            FROM users WHERE email = p_email $$;

    CREATE FUNCTION auth_refresh_owner(p_token_hash text)
      RETURNS TABLE (id uuid, user_id uuid, family_id uuid, expires_at timestamptz,
                     used_at timestamptz, revoked_at timestamptz)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
      AS $$ SELECT id, user_id, family_id, expires_at, used_at, revoked_at
            FROM refresh_tokens WHERE token_hash = p_token_hash $$;

    CREATE FUNCTION auth_reset_owner(p_token_hash text)
      RETURNS TABLE (id uuid, user_id uuid, expires_at timestamptz, used_at timestamptz)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
      AS $$ SELECT id, user_id, expires_at, used_at
            FROM password_reset_tokens WHERE token_hash = p_token_hash $$;

    -- Serialised by the caller with an advisory lock: first registered user becomes admin.
    CREATE FUNCTION auth_user_count() RETURNS bigint
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
      AS $$ SELECT count(*) FROM users WHERE NOT is_synthetic $$;

    REVOKE ALL ON FUNCTION auth_lookup_user(citext), auth_refresh_owner(text),
      auth_reset_owner(text), auth_user_count() FROM PUBLIC;
    """)
    op.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpilot_app') THEN
        GRANT EXECUTE ON FUNCTION auth_lookup_user(citext), auth_refresh_owner(text),
          auth_reset_owner(text), auth_user_count() TO finpilot_app;
      END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DROP VIEW IF EXISTS v_transactions_reporting;
    DROP FUNCTION IF EXISTS auth_lookup_user(citext), auth_refresh_owner(text),
      auth_reset_owner(text), auth_user_count();
    DROP TABLE IF EXISTS transactions, accounts, user_settings, auth_events,
      password_reset_tokens, refresh_tokens, users CASCADE;
    DROP POLICY IF EXISTS tenant_read ON llm_calls;
    ALTER TABLE llm_calls DISABLE ROW LEVEL SECURITY;
    DROP FUNCTION IF EXISTS set_updated_at(), app_current_user_id();
    """)
