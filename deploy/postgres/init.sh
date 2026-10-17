#!/usr/bin/env bash
# Runs once on first boot of finpilot-postgres (docker-entrypoint-initdb.d).
# Creates: app role (non-owner, RLS applies), admin role (BYPASSRLS, system jobs),
# read-only dev role (Postgres MCP), the `langfuse` database, and extensions.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="${FINPILOT_APP_DB_PASSWORD:-finpilot_app}" \
  -v admin_pw="${FINPILOT_ADMIN_DB_PASSWORD:-finpilot_admin}" \
  -v ro_pw="${FINPILOT_RO_DB_PASSWORD:-finpilot_ro}" <<'SQL'
CREATE ROLE finpilot_app   LOGIN PASSWORD :'app_pw'   NOSUPERUSER NOBYPASSRLS;
CREATE ROLE finpilot_admin LOGIN PASSWORD :'admin_pw' NOSUPERUSER BYPASSRLS;
CREATE ROLE finpilot_ro    LOGIN PASSWORD :'ro_pw'    NOSUPERUSER NOBYPASSRLS;
ALTER ROLE finpilot_ro SET default_transaction_read_only = on;
CREATE DATABASE langfuse;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
GRANT CONNECT ON DATABASE finpilot TO finpilot_app, finpilot_admin, finpilot_ro;
SQL
