# FinPilot — all targets go through uv / docker compose.
.DEFAULT_GOAL := help
SHELL := /bin/bash
DC ?= docker compose
DB ?= finpilot
PGUSER_ := $${POSTGRES_USER:-finpilot}
BACKUP_DIR ?= backups
BACKUP_FILE ?= $(BACKUP_DIR)/$(DB)-$(shell date +%Y%m%d-%H%M%S).dump

.PHONY: help install up up-deps down migrate seed-demo run worker beat fx-mcp test test-live lint fmt \
        typecheck prompts-sync eval eval-offline export-reqs check-reqs db-backup db-restore db-verify-backup prod-secrets prod-check prod-up prod-down mesh-probe hello-trace langfuse-reset-keys e2e eval-report

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

install: ## uv sync (with dev deps)
	uv sync

up: ## Start the full stack (app, worker, beat, fx-mcp, postgres, redis, langfuse)
	$(DC) up -d --build
	@echo "FinPilot → http://localhost:$${APP_PORT:-8000}   Langfuse → http://localhost:$${LANGFUSE_PORT:-3000}"

up-deps: ## Start only Postgres + Redis (+ fx-mcp) for local dev/tests
	$(DC) up -d --wait finpilot-postgres finpilot-redis

down: ## Stop the stack
	$(DC) down

migrate: ## Apply Alembic migrations
	uv run alembic upgrade head

seed-demo: ## 4 demo accounts (admin@, inr@, usd@, mixed@finpilot.local) with sample data from data/sample/
	uv run python -m scripts.seed_demo

run: ## Run the API locally with reload
	uv run uvicorn finpilot.api.main:app --reload --port $${APP_PORT:-8000}

worker: ## Run a Celery worker locally
	uv run celery -A finpilot.tasks.celery_app worker --loglevel=INFO

beat: ## Run Celery beat locally
	uv run celery -A finpilot.tasks.celery_app beat --loglevel=INFO

fx-mcp: ## Run the FX MCP server locally on :8765
	uv run python -m mcp_servers.fx.server

test: ## Run tests (needs `make up-deps` for db/redis tests)
	uv run pytest -q

test-live: ## Run live tests (Mesh, FX source) — needs keys/network
	uv run pytest -q -m live

lint: ## ruff + format check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy finpilot mcp_servers eval

fmt: ## Auto-format
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy finpilot mcp_servers eval

prompts-sync: ## Push prompt files to Langfuse Prompt Management (files stay the source of truth)
	uv run python -m scripts.prompts_sync

eval: ## Run all eval suites incl. LLM suites (Mesh; a few cents), then RESULTS.md
	uv run python -m eval.runner --mode full --gate
	uv run python -m eval.report

eval-offline: ## Deterministic eval suites with threshold gating
	uv run python -m eval.runner --mode offline --gate

eval-report: ## Regenerate eval/results/RESULTS.md from the latest runs in Postgres
	uv run python -m eval.report

export-reqs: ## Regenerate requirements.txt from uv.lock
	uv export --no-hashes --no-dev --format requirements-txt -o requirements.txt

check-reqs: ## Fail if requirements.txt is out of sync with uv.lock
	@uv export --no-hashes --no-dev --format requirements-txt -o /tmp/finpilot-reqs.txt >/dev/null
	@diff -q <(grep -v '^#' requirements.txt) <(grep -v '^#' /tmp/finpilot-reqs.txt) && echo "requirements.txt in sync"

db-backup: ## pg_dump DB (default finpilot; DB=langfuse too) in custom format into ./backups
	@mkdir -p $(BACKUP_DIR)
	$(DC) exec -T finpilot-postgres pg_dump -U $(PGUSER_) -d $(DB) -Fc > $(BACKUP_FILE)
	@echo "wrote $(BACKUP_FILE) ($$(du -h $(BACKUP_FILE) | cut -f1))"

db-restore: ## Restore FILE=backups/<file>.dump into DB (replaces its objects; stops app/worker/beat meanwhile)
	@test -n "$(FILE)" || (echo "usage: make db-restore FILE=backups/x.dump [DB=finpilot]" && exit 1)
	$(DC) stop app worker beat
	$(DC) exec -T finpilot-postgres pg_restore -U $(PGUSER_) -d $(DB) --clean --if-exists --single-transaction < $(FILE)
	$(DC) start app worker beat

db-verify-backup: ## Restore FILE= into a scratch database and compare row counts with the live DB
	@test -n "$(FILE)" || (echo "usage: make db-verify-backup FILE=backups/x.dump" && exit 1)
	$(DC) exec -T finpilot-postgres dropdb -U $(PGUSER_) --if-exists finpilot_restore_check
	$(DC) exec -T finpilot-postgres createdb -U $(PGUSER_) finpilot_restore_check
	$(DC) exec -T finpilot-postgres pg_restore -U $(PGUSER_) -d finpilot_restore_check --exit-on-error < $(FILE)
	@for t in users transactions categories recurring_rules goals recommendations memories digests fx_rates; do \
	  a=$$($(DC) exec -T finpilot-postgres psql -U $(PGUSER_) -d $(DB) -tAc "SELECT count(*) FROM $$t"); \
	  b=$$($(DC) exec -T finpilot-postgres psql -U $(PGUSER_) -d finpilot_restore_check -tAc "SELECT count(*) FROM $$t"); \
	  printf "  %-16s live %-7s restored %-7s %s\n" $$t $$a $$b $$([ "$$a" = "$$b" ] && echo ok || echo "DIFFERS (writes since the backup?)"); \
	done
	$(DC) exec -T finpilot-postgres dropdb -U $(PGUSER_) finpilot_restore_check

langfuse-reset-keys: ## Re-create the Langfuse project API key from current .env (after changing LANGFUSE_SALT)
	$(DC) exec -T finpilot-postgres psql -U $${POSTGRES_USER:-finpilot} -d langfuse -c "DELETE FROM api_keys WHERE public_key = '$${LANGFUSE_PUBLIC_KEY:-pk-lf-finpilot-local}'"
	$(DC) restart langfuse-web

e2e: ## Browser smoke test (Playwright) against the running stack (seeds the demo accounts)
	uv run python -m scripts.seed_demo >/dev/null
	uv run --no-project --with playwright==1.55.0 python -m playwright install chromium >/dev/null
	uv run --no-project --with playwright==1.55.0 python scripts/e2e_smoke.py e2e-shots

a11y: ## Accessibility audit (axe-core, WCAG 2.1 AA) of every page in light and dark mode
	uv run python -m scripts.seed_demo >/dev/null
	uv run --no-project --with playwright==1.55.0 python -m playwright install chromium >/dev/null
	uv run --no-project --with playwright==1.55.0 python scripts/a11y_audit.py

PROD = $(DC) -f docker-compose.yml -f docker-compose.prod.yml --profile prod

prod-secrets: ## Print fresh random values for every production secret (append to .env)
	@python3 scripts/prod_env.py generate

prod-check: ## Fail if .env still contains dev-default or weak secrets
	@python3 scripts/prod_env.py check

prod-up: prod-check ## Production stack behind Caddy (HTTPS); see DEPLOY.md
	$(PROD) up -d --build

prod-down: ## Stop the production stack
	$(PROD) down

mesh-probe: ## Verify Mesh chat + embeddings with your MESH_API_KEY
	uv run python -m scripts.mesh_probe

hello-trace: ## Send one traced Mesh call and print the Langfuse trace
	uv run python -m scripts.hello_trace
