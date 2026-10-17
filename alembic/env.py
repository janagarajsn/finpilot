"""Alembic environment. Runs as the schema-owner role (DATABASE_MIGRATION_URL) and holds a
Postgres advisory lock so concurrent app/worker starts never migrate twice."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool, text

from alembic import context
from finpilot.config import get_settings

MIGRATION_LOCK_ID = 727_001  # arbitrary, stable

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # DDL is authored by hand (Postgres-first); no autogenerate


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_migration_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        conn.commit()
        try:
            context.configure(connection=conn, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            conn.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
