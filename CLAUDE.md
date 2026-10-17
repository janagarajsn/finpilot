# CLAUDE.md — FinPilot

Re-read this file at the start of every session. Spec: `docs/build-process/BUILD_PROMPT.md` + `docs/build-process/AMENDMENT_1.md` (amendment wins on conflict). Merged plan & status: `docs/build-process/PLAN.md`. Decisions & verified third-party facts: `DESIGN_NOTES.md`.

## What this is
Multi-user, proactive, multi-agent personal-finance app (hackathon). FastAPI + Jinja/htmx UI, PostgreSQL 16 (pgvector, RLS) as the only database, Redis, Celery + beat, LangGraph (Redis checkpointer), Langfuse v3, all LLM calls via Mesh API. Base currency INR; users pick a reporting currency (INR|USD); FX via our own FX MCP server with a fallback chain.

## Hard rules (tests enforce most of these)
1. **Mesh only.** `finpilot/llm/client.py` is the ONLY file allowed to import `openai` / `langfuse.openai` / any model SDK or contain an LLM base URL. Embeddings too. (`tests/test_mesh_only.py`)
2. **Numbers come from code.** LLM classifies, parses, picks among computed candidates, phrases, judges. Every number in LLM text passes `trail/grounding.py`. The LLM never converts currency or invents a rate.
3. **No prompt text in `.py`.** Prompts → `prompts/<agent>/<name>.v<N>.md`; taxonomies/lexicons/intents → `config/*.yml`; fallback text → `templates/messages/*.j2`. (`tests/test_no_inline_prompts.py`)
4. **Untrusted data** (descriptions, notes, custom category text, CSV, memories, MCP tool output) goes inside `<data>…</data>`; outputs are schema-validated.
5. **Tenancy.** Every repo function takes `user_id` from the authenticated principal; every user table has `user_id` + RLS policy; Redis/cache/memory keys are user-scoped. Requests/tasks run inside a transaction with `SET LOCAL app.user_id`.
6. **Postgres-first.** Real DDL lives in Alembic migrations (raw SQL via `op.execute` is fine and preferred for Postgres features). uuid PKs (`gen_random_uuid()`), identity bigint for log tables, `NUMERIC(18,2)` money, `NUMERIC(18,8)` rates, `timestamptz`, `citext` email, JSONB, CHECKs, partial unique indexes, `ON DELETE CASCADE` from users. Heavy analytics = `.sql` files in `finpilot/db/sql/`, loaded via helper; reporting reads `v_transactions_reporting`.
7. **Money**: `Decimal` at boundaries, round-half-even; floats only inside pandas/numpy.
8. **Secrets** only in `.env` (gitignored). Never in code, `.mcp.json`, or commits.
9. **MCP**: tools must be in `config/mcp_servers.yml` `allowed_tools`; MCP calls are not LLM calls.

## Commands (uv everywhere)
```bash
uv sync                         # install (dev included)
uv run pytest -q                # tests (needs Postgres+Redis: `make up-deps`)
uv run ruff check . && uv run ruff format --check .
uv run mypy finpilot mcp_servers eval   # same scope as CI / make lint
uv run alembic upgrade head     # migrations
uv run uvicorn finpilot.api.main:app --reload   # local dev (make run)
uv export --no-hashes --format requirements-txt > requirements.txt   # make export-reqs
docker compose up --build       # full stack
make help                       # all targets
```

## Where things live
| Path | Purpose |
|---|---|
| `finpilot/config.py` | pydantic-settings `Settings` (all env vars) |
| `finpilot/llm/client.py` | Mesh client (Langfuse-wrapped), retries, JSON/tool helpers, embeddings, LLM cache hooks |
| `finpilot/llm/prompt_registry.py` | loads/validates/renders prompt files (STATIC first) |
| `finpilot/observability/` | Langfuse init (no-op fallback), masking, scores |
| `finpilot/fx/` | FX provider (HTTP adapter shared with MCP server), resolver, fallback chain |
| `finpilot/mcp/` | MCP client (timeouts, retries, circuit breaker, Redis cache) + registry |
| `mcp_servers/fx/` | FastMCP FX server (compose service `fx-mcp`) |
| `finpilot/db/` | session (`user_session` = RLS `SET LOCAL`, `system_session`), repos, `sql/*.sql`, `llm_log.py` |
| `finpilot/tenancy.py` | `Principal` from Bearer/cookie, `get_principal`, `get_db` (tenant-scoped session), `require_admin` |
| `finpilot/security/` | passwords (argon2id), tokens (JWT + HMAC'd opaque), csrf (double-submit), ratelimit (Redis), headers (CSP) |
| `finpilot/auth/service.py` | register/login/lockout/refresh rotation+reuse/logout/change/forgot/reset |
| `finpilot/categories/` | `taxonomy.py` (config/taxonomy.yml), `seed.py` (system sync), `service.py` (CRUD, merge, keywords, suggest, recategorize, `CategoryResolver`) |
| `finpilot/transactions/service.py` | manual entry, FX on write, edits, merchant memory corrections, FX override, list/export |
| `finpilot/recurring/` | `rules.py` (pure schedule math), `service.py` (CRUD/lifecycle), `materializer.py` (tick), `detect.py` (detected series), `detected.py` (promote/dismiss/usage) |
| `finpilot/ingest/` | `normalizer.py` (config/normalizer.yml), `csv_parser.py`, `template.py` (templates/csv/), `import_service.py` |
| `finpilot/insights/dashboard.py` + `finpilot/db/sql/*.sql` | deterministic dashboard aggregates (`sql_loader.sql(name)`) |
| `finpilot/tasks/` | `celery_app.py` (beat schedule), `fx_tasks.py`, `ledger_tasks.py` (materializer tick, imports), `locks.py` |
| `finpilot/money.py` | `Money`, currency registry (`config/currencies.yml`), Babel formatting |
| `finpilot/api/` | `main.py` (app factory, middleware), `routes_*.py` (JSON API), `schemas.py`, `cookies.py` |
| `finpilot/web/` | `views.py` (pages + htmx form handlers), `templates/`, `static/` (no inline JS: CSP) |
| `alembic/` | DDL migrations |
| `finpilot/agents/categorizer.py` | layered categorizer (memory → keywords → lexicon → LLM), flow guard, degraded mode |
| `finpilot/forecasting/` | `model.py` (pure Monte Carlo engine, common random numbers, naive baselines), `inputs.py` (DB → ForecastInputs: declared ∪ detected schedule, variable streams, stored FX), `scenarios.py` (typed what-ifs → inputs), `simulator.py` (verdict rule, verified levers), `goals.py`, `service.py` (`get_forecast` snapshot in `forecast_runs`, `simulate_scenario`) |
| `finpilot/agents/coordinator.py` + `finpilot/tasks/pipeline_tasks.py` | LangGraph pipeline (route_intent → ingest → categorize → analyze → forecast → goals → advise → persist_trail → snapshot → deliver) on the Redis checkpointer; `agent_runs` + `run:{id}` progress + Langfuse spans; degraded mode; `run_pipeline` (per-user lock), `schedule_pipeline` (debounced), `resume_stale_runs` |
| `finpilot/agents/goal_tracker.py` | weekly goal snapshots + deltas, `goal_ahead` findings |
| `finpilot/agents/simulator_graph.py` | NL what-if: parse (prompt `simulator_parser/parse`) → `interrupt()` clarification on the Redis checkpointer (DB 0, prefix `LANGGRAPH_KEY_PREFIX`) → simulate → `templates/messages/simulation.yml` |
| `finpilot/trail/` | `grounding.py` (extract ₹/$/lakh/crore/%/dates, currency-aware FactSet, `guarded_json` retry → fallback), `narration.py` (what-if answers, Insights summary behind the guard) |
| `finpilot/advice/candidates.py` + `finpilot/agents/advisor.py` | deterministic candidates (simulated effects, finding ids, entities) → smart LLM picks top 3 → validator + scoped grounding → `recommendations` + `decision_trail`; dismissal memory by candidate key |
| `finpilot/agents/analyzer.py` + `finpilot/insights/` | deterministic analyzer: `frame.py` (pandas input via `db/sql/analysis_txns.sql`, `category_monthly.sql`), `anomalies.py`, `patterns.py`, `subscriptions.py`, `fx_exposure.py`, `models.py` (`AnalysisReport`, `Finding`), `text.py` (renders `templates/messages/findings.yml`); `get_report` = snapshot or recompute under an advisory lock |
| `finpilot/categories/rules.py` + `config/rules_lexicon.yml` | sign-aware rule lexicon; `config/thresholds.yml` holds categorizer thresholds + eval gates |
| `finpilot/agents/intent_router.py` + `config/intents.yml` | question → (intent, period): rules → embeddings → fast LLM → free_form |
| `finpilot/answers/` | `facts.py` (deterministic facts per intent), `service.py` (L2 `cache:ans:` / L3 `semantic_cache`, single-flight, prewarm, `answer_log`) |
| `finpilot/memory/` | `long_term.py` (`SemanticMemory` protocol, `PgMemory`: write rules merge/supersede/blocked, lexical fallback), `retrieval.py` (scoring, `recall`), `short_term.py` (Redis window + rolling summary), `writer.py` (chat consolidation, decision memories), `config.py` (config/memory.yml) |
| `finpilot/agents/chat_agent.py` + `chat_tools.py` + `config/chat_tools.yml` | chat paths (pending what-if → simulator → answer cache → Mesh tool loop), tools, grounding guard + headline fallback, sessions/feedback; `/chat`, `/chat/stream` (SSE), `/memory` |
| `finpilot/digests/` + `finpilot/delivery/` | `schedule.py` (pure slot math: local weekday/hour, period_key, 24 h window), `service.py` (compose → grounded phrasing → deliver; idempotent per period), `alerts.py` (low balance, 7-day dedupe); adapters `email.py`, `telegram.py` (Bot API, linking, webhook/poll), `log_sink.py` (outbox/) |
| `finpilot/tasks/digest_tasks.py` + `jobs.py` | beat ticks (digests_due, risk_alerts, refresh_insights, goal_snapshots, cache_gc, telegram_poll); `job_runs` via Celery signals + `status()` for `GET /scheduler/jobs` |
| `data/sample/` + `finpilot/ingest/sample_data.py` | committed sample datasets (INR, USD, INR+USD; import-template CSVs + `datasets.yml`), "Load sample data" (`GET/POST /me/sample-data`, undo removes its recurring items); rebuild with `scripts/build_sample_data.py`; `make seed-demo` = admin@/inr@/usd@/mixed@finpilot.local. Eval suites use `eval/synth/persona_sample.py` instead |
| `finpilot/privacy/pii.py` + `config/privacy.yml` | Presidio PII masking (tokenizer-only spaCy, no model download): every Mesh prompt and embedding input (`LLMClient._mask`), Langfuse payloads, and the output leak check in `trail/grounding.check` (`pii` → retry → fallback). Names are not masked (categorization needs them) |
| `finpilot/trail/scope.py` + `config/guardrails.yml` | advice scope: no specific investment / tax / legal directives (chat tool loop + advisor validator); chat scope intents `off_topic`/`smalltalk` in `config/intents.yml` + `decline_off_topic` tool |
| `finpilot/account_data.py` | `GET /me/export` JSON (all user tables, no secrets/vectors), account deletion (password, cascade, anonymous audit) |
| `docker-compose.prod.yml` + `deploy/caddy/Caddyfile` + `scripts/prod_env.py` | production: Caddy HTTPS only entry point, `make prod-secrets/prod-check/prod-up`; see DEPLOY.md |
| `scripts/a11y_audit.py` | `make a11y`: axe-core WCAG 2.1 AA on every page in light and dark mode |
| `finpilot/llm/cache.py` | Redis L1 cache (`cache:llm:{scope}:…`; scope = user id, or `eval:<persona>`) |
| `finpilot/agents/` | (later phases) analyzer, forecaster, simulator, advisor, goal tracker, chat, intent router, coordinator |
| `finpilot/forecasting/`, `trail/`, `insights/`, `memory/`, `ingest/`, `recurring/`, `categories/` | domain modules |
| `finpilot/tasks/` | Celery app, beat schedule, tasks |
| `finpilot/api/`, `finpilot/web/` | FastAPI routes, Jinja templates/static |
| `prompts/`, `config/`, `templates/` | all prompt/config/message text |
| `eval/` | `synth/` (personas, merchants.yml, generator), `loader.py`, `suites/` (categorization, anomalies, recurring, forecast, simulation, advisor, cache, memory, fx), `runner.py`, `store.py`, `insights.py`, `langfuse_sync.py`, `report.py` → `eval/results/RESULTS.md` |
| `deploy/langfuse/`, `deploy/caddy/`, `deploy/postgres/` | infra |
| `scripts/` | one-off probes (Mesh probe, hello-trace) |

## Conventions
- Python 3.12, type hints, Pydantic v2 models at boundaries. ruff (line length 100), mypy lenient.
- Sync SQLAlchemy 2.x + psycopg3. MCP client is async; wrap with `anyio.from_thread`/`asyncio.run` at sync boundaries.
- Prompt file frontmatter: `name, version, model_tier (fast|smart), temperature, response_schema, input_variables, cache_ttl`; body has `{# STATIC #}` then `{# DYNAMIC #}`.
- Redis key namespaces: `rl:`, `sess:`, `lg:`, `chat:`, `cache:llm:`, `cache:ans:`, `cache:lock:`, `dv:`, `run:`, `evalrun:`, `lock:`, `fx:`, `mcp:`, `tg:` (Telegram polling offset). DB 0 app, 1 broker, 2 results.
- Tests: real Postgres + Redis (`make up-deps`); the session fixture **re-creates DB `finpilot_test`** from migrations and uses Redis DB 15, so dev data is never touched. Tests never read `.env` (`FINPILOT_ENV_FILE=""`). LLM via `FakeBackend`, HTTP via `respx`, MCP via in-process FastMCP (`server._mcp_server`). Live-network tests: `@pytest.mark.live` (skipped by default). Helpers: `tests/helpers.py`; IDOR cases: register in `tests/idor.py::CASES`.
- New user-owned table ⇒ add `user_id` + RLS policy in its migration (the RLS meta-test enforces this). New route ⇒ requires auth unless added to `PUBLIC` in `tests/test_idor.py`.
- Pre-auth DB access only via SECURITY DEFINER functions; system jobs use `system_session` / admin role.
- Never bump `data_version` in Python: the triggers do it. New financial-fact tables need the three `*_dv_*` triggers (see migration 0003). DERIVED tables (detected_recurrences, analysis_findings, insight_snapshots) must NOT have them; user actions on derived rows call `analyzer.invalidate()`.
- Forecast randomness: every random draw is keyed (`SeedSequence([seed, …, stable_hash(key)])`) so scenarios never perturb other draws; item keys use stable ids (series_key), and evals pass persona-based seeds. Keep it that way or the simulator properties break.
- Pandas 3 stores missing strings as NaN: use `pd.isna(x)`, never `x is None`, on DataFrame values. Values that round-trip through JSON (snapshots, eval details) must be typed models or plain Python numbers (numpy scalars become strings).
- Pipeline nodes open their OWN `user_session` (one transaction per node) so checkpoints match durable state; never hold one session across nodes. A node that needs the LLM uses `_llm(state, c)` (None in degraded mode) and must flip `degraded` on an LLM failure instead of failing the run.
- LLM text with numbers ALWAYS goes through `trail.grounding.guarded_json` with a FactSet built by code (scoped to what the text is about) and a deterministic fallback in `templates/messages/`. Never show unguarded model text containing numbers.
- Every analyzer finding states metric/baseline/observed/threshold + evidence txn ids, has a template in `templates/messages/findings.yml` (test enforces) and never says "unused" (only "likely low value (heuristic)" or "you rated it rarely used").
- UI colours: TEXT uses the semantic tokens `text-muted|soft|accent|pos|neg|warn` (CSS variables in `static/css/theme.css`, light + dark, all ≥ 4.5:1), never raw slate/brand/emerald/rose text shades; tinted backgrounds need a `dark:bg-*` partner; scroll containers get `tabindex="0"`. Navigation lives in `views.NAV` (grouped sidebar, `partials/sidebar.html`; tools carry an admin-only flag, and Pipeline/Quality pages use `_admin_required`), icons in `partials/icons.html`. Charts call `window.fpChartTheme()`. After UI changes run `make e2e` AND `make a11y` (axe, light + dark, must report 0).
- UI wording (the tool's own text only; never reformat user data or model output): sentence case for headings, buttons, labels and captions (a caption's first word capitalised: "All accounts", "Bad case · 31 Dec"); text roles from `theme.css`: `.eyebrow` (KPI labels, group labels, every `<thead>`), `.badge` (status chips, uppercase), `.note` (italic provenance/estimates: "AI-written, numbers checked", "expected"), `.qualifier` (lighter part of a heading: "Where it went · Last 30 days", a period label keeps its own casing). Dates the tool shows use `|day` / `window.fpDay` ("15 Oct", year when not this year) and `|month` ("Oct 2026"); never show raw codes (`in_app`, `manual`).
- UI JS lives in `finpilot/web/static/js/*.js` (Alpine components registered on `alpine:init`); pass data via `<script type="application/json">` + `tojson`; single-quote attributes that embed `tojson`.
- `make e2e` = Playwright smoke test against the running stack (0 console errors required). Run it after UI changes.
- DB dependency in routes: ALWAYS `Depends(get_db, scope="function")` (commit before response; meta-test enforces).
- Answer cache: never bump or scan keys; any new input to an answer goes INTO the L2 key (`answers/service.py::l2_key`). Never cache degraded or user-amount answers.
- Memory: never store figures FinPilot computed or anything matching `config/memory.yml` blocked patterns; same kind+topic = supersede (code decides, not the LLM). Memories go into prompts only in the DYNAMIC section inside `<memory>` tags. Deleting a memory deletes its superseded history.
- Guardrails: never send raw user text to Mesh outside `LLMClient` (it masks PII); new outward-facing LLM text goes through `check()` (numbers + PII) and, if it can advise, `scope.violations()`. Guardrail outcomes are Langfuse scores (`pii_masked`, `pii_leak_blocked`, `advice_scope_blocked`, `grounding_pass`). The `guardrails` eval suite gates all of it; add new identifier/directive cases to `eval/datasets/guardrails.yml`.
- Chat: every number in a tool-loop answer must come from a tool result (FactSet) or the user's message; tool output goes back inside `<data>`. New tools: schema + description in `config/chat_tools.yml`, implementation in `chat_tools.TOOLS`, progress/headline text in `templates/messages/chat.yml`.
- Scheduler: add TICKS (one beat entry that selects users), never per-user schedules; every tick takes `redis_lock("beat:<name>")` and excludes `is_synthetic` users. Anything delivered must be idempotent (UNIQUE key or dedupe window). Settings that change computed results (e.g. cash buffer) must invalidate the snapshots that used them.
- Deploy: anything that must not ship in images goes in `.dockerignore` (backups/, outbox, .env). New secrets with a dev default must be added to `scripts/prod_env.py` so `make prod-check` refuses the default.
- Evals: `make eval-offline` (free, gated, CI), `make eval` (Mesh, a few cents) then `make eval-report`. New suites register in `eval/runner.py::SUITES` and gates in `config/thresholds.yml` (`min`/`max`; add lower-is-better metrics to `eval/insights.py::LOWER_IS_BETTER`). Never hand-edit RESULTS.md.
- Scripted multi-line text replacements silently miss after `ruff format`: edit formatted files with the Edit tool (it fails loudly) or re-read first.
- Workflow: phase by phase per `docs/build-process/PLAN.md`; tests green → update its checkboxes → commit (`Phase N: …`). Ambiguity → simplest option, record in DESIGN_NOTES.md.
- Third-party APIs (Langfuse, LangGraph Redis, pgvector, Celery, Mesh, MCP SDK, Frankfurter): check current docs, pin versions, record in DESIGN_NOTES.md.

## Dev tooling (dev-time MCP, NOT part of the app)
`.mcp.json` at repo root configures, for Claude Code: Context7 (library docs), Postgres MCP (LOCAL dev DB, read-only role `finpilot_ro`), Playwright MCP (UI smoke), GitHub MCP, Langfuse docs MCP. Secrets come from env vars (`${CONTEXT7_API_KEY}`, `${GITHUB_PAT}`, `${DEV_DATABASE_URI_RO}`), never literals.
