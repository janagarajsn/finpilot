# DESIGN_NOTES

Decisions, trade-offs, and third-party facts verified against current official docs. Newest first within each section.

## Decisions at a glance

The sections below are a phase-by-phase log. This index points to where each decision and its reasons live.

| Decision | Choice and why | Where |
|---|---|---|
| Categorization | **Hybrid, not pure LLM:** merchant memory → rule lexicon → fast LLM over unique unresolved merchants. Cheaper (most rows never reach the LLM), reproducible, explainable per row, and accuracy is measured per layer. | §5 |
| Numbers vs language | **Deterministic numerics; LLM for language only.** Every number shown is computed by code or passes the grounding guard; otherwise a template is shown. | §8, §10, §11 |
| Forecasting | **Monte Carlo bootstrap over the user's own residuals**, not ARIMA/Prophet. Months of noisy personal data with scheduled bills and paydays need a distribution (p10/p50/p90 and "chance below the cushion"), not a point series. Scenarios reuse the same random draws, so differences are exact. Backtested against two naive baselines. | §7 |
| Grounding guard | Currency-aware number/date/percent matching against a FactSet built by code, with 2 named retries, then fallback. Tested with corruption injection. | §8 |
| Vector store | **pgvector in Postgres, not Qdrant:** one fewer service, transactional with user data, RLS-scoped search, trivial delete-by-user. A `SemanticMemory` protocol leaves room for an adapter. | §11 |
| Scheduling | **Celery beat ticks, not per-user cron.** A fixed set of beat entries select users each tick; idempotency comes from UNIQUE keys and Redis locks, so restarts and replicas are safe. | §12 |
| Redis roles | Cache (`cache:llm:`, `cache:ans:`), locks, rate limits, LangGraph checkpoints, short-term chat window, progress. Everything except in-flight checkpoints can be rebuilt from Postgres. | §2, §9, §10, §11 |
| Cache invalidation | **By version, never by scanning:** keys include `data_version` (bumped by DB triggers on financial facts), reporting currency, prompt/template version, model, date bucket and FX as-of. | §6, §10 |
| Prompts | **Versioned files** (`prompts/<agent>/<name>.vN.md`, STATIC then DYNAMIC); a test forbids prompt text in `.py`; `make prompts-sync` mirrors them to Langfuse. | §0, §2 |
| Multi-tenancy | **Three layers:** RLS on every user table (`app.user_id` per transaction), repository functions that take the principal's user id, user-scoped cache/memory keys. IDOR and RLS meta-tests guard all three. | §3 |
| Privacy & masking | Hashed user ids in traces, untrusted text inside `<data>`, no credentials/account numbers in memory, export and delete-my-data. | §3, §11, §13 |
| Currency model | INR base with any transaction currency; the rate of the day is stored on each row; the reporting currency is a view (INR or USD). Rates come from the FX MCP server → HTTP → stored → pinned. | §3, §13 |
| Self-evaluation | Seeded personas with ground truth, nine suites, gates in `config/thresholds.yml`, results in Postgres + Langfuse + the Quality page, every tuning on eval data disclosed, held-out personas for the tuned analyzer. | §5, §6, §7, §8, §10, §11, §13, §14 |

## DATA MODEL (PostgreSQL schema)

PostgreSQL 16 is the only database. DDL lives in Alembic migrations as raw SQL. Conventions:
- uuid primary keys (`gen_random_uuid()`); identity bigint for log tables;
- `NUMERIC(18,2)` money, `NUMERIC(18,8)` rates, `timestamptz`, `citext` email, JSONB, CHECKs, partial unique indexes;
- `ON DELETE CASCADE` from `users`;
- RLS on every user table, with policy `user_id = app_current_user_id()`.

| Area | Tables |
|---|---|
| Identity | `users`, `user_settings`, `refresh_tokens`, `password_reset_tokens`, `auth_events`, `telegram_link_tokens` |
| Ledger (financial facts, bump `data_version` via triggers) | `accounts`, `transactions` (original amount + currency, `amount_inr`, rate, rate date, FX source), `categories` (system rows `user_id NULL`), `hidden_categories`, `merchant_memory`, `recurring_rules`, `import_batches`, `import_files`, `goals` |
| Derived (no `data_version` triggers; invalidated explicitly) | `detected_recurrences`, `analysis_findings`, `insight_snapshots`, `forecast_runs`, `goal_snapshots`, `recommendations`, `decision_trail` |
| Agents & runs | `pipeline_runs`, `agent_runs`, `llm_calls`, `answer_log`, `semantic_cache` (vector 1536, HNSW) |
| Chat & memory | `chat_sessions`, `chat_messages`, `memories` (vector 1536 + HNSW, tsvector + trigram for the lexical fallback, `superseded_by`) |
| Proactive | `digests` (UNIQUE user + period), `alerts` (7-day dedupe) |
| System (no user data, no RLS) | `fx_rates` (real vs synthetic scope), `fx_fetch_log`, `job_runs`, `eval_runs`, `eval_metrics`, `eval_cases`, `eval_insights` |
| Views | `v_transactions_reporting` (security invoker: amounts in the user's reporting currency at the stored rate of the day) |

Pre-authentication access goes only through SECURITY DEFINER functions: the auth lookups, and `telegram_bind` for the bot webhook. System jobs use the BYPASSRLS admin role with an audit reason.

## 0. Verified third-party facts (Phase 0, checked 2026-10-15)

### Pinned versions (resolved by `uv`, locked in `uv.lock`)
fastapi 0.143.2 · sqlalchemy 2.1.4 · alembic 1.20.0 · psycopg 3.3.6 · pgvector 0.5.1 · redis-py 8.1.0 · celery 5.6.3 · langgraph 1.2.15 · langgraph-checkpoint-redis 0.5.2 · langfuse 5.0.1 · openai 3.31.0 · mcp 2.3.0 · fastmcp 4.1.0 · pydantic 2.14.1 · pydantic-settings 2.16.0 · httpx 0.28.1 · respx 0.23.1 · pytest 9.1.1 · hypothesis 6.169.4 · ruff 0.17.1 · mypy 2.4.0 · pandas 3.0.6 · numpy 2.5.4.

### Langfuse: server v4 + Python SDK v5 (deviation from the spec's "v3")
- The spec asks for "Langfuse v3 SDK". As of today, OSS server **v3 is deprecated** (security patches until 2027-01-31) and the official `docker-compose.yml` runs `langfuse/langfuse:4` + `langfuse-worker:4`. Python SDK **v5.0.1** requires server ≥ 4.59.0. Source: https://langfuse.com/self-hosting/upgrade/versioning
- **Decision:** self-host server **v4** and use SDK **v5**. The spec's intent (OTel-based SDK: `@observe`, `get_client()`, `from langfuse.openai import OpenAI`, `from langfuse.langchain import CallbackHandler`, datasets + experiments) is unchanged in v5. Verified locally by importing the installed package.
- API notes that matter to us: trace attributes via the `propagate_attributes(user_id=, session_id=, tags=, metadata=, trace_name=)` context manager (metadata values must be `str`, ≤ 200 chars); datasets via `get_dataset(...).run_experiment(name=, task=)` (task signature `task(*, item, **kw)`); scores via `create_score` / `score_current_trace`. `item.run()` and `get_dataset_runs()` were removed.
- Langfuse Cloud works by changing only `LANGFUSE_HOST` + keys.
- **Verified on our stack (server 4.59.0):** v4 runs in "events_only" mode, so `GET /api/public/traces/{id}` is unavailable. Read traces with `GET /api/public/v2/observations?traceId=…` (SDK: `api.observations`). Our Phase 0 span arrived with the hashed `userId` and the `sessionId`. Eval deep links use `/project/<id>/traces/<traceId>` in the UI.
- Headless init variables (on `langfuse-web`): `LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_ORG_NAME`, `LANGFUSE_INIT_PROJECT_ID`, `LANGFUSE_INIT_PROJECT_NAME`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY`, `LANGFUSE_INIT_USER_EMAIL`, `LANGFUSE_INIT_USER_NAME`, `LANGFUSE_INIT_USER_PASSWORD`. Source: https://langfuse.com/self-hosting/administration/headless-initialization
- Vendored compose: `deploy/langfuse/docker-compose.yml`, adapted from the official main-branch file. Changes: services renamed with a `langfuse-` prefix; it reuses our `finpilot-postgres` (database `langfuse`) instead of its own Postgres; host ports for ClickHouse, Redis and MinIO are removed or remapped to avoid collisions.

### Mesh API
- OpenAI-compatible at `https://api.meshapi.ai/v1`; model ids are `<brand>/<model>`. Source: https://developers.meshapi.ai
- `/v1/embeddings` **is documented and live-verified** (2026-10-15, `make mesh-probe`): `openai/text-embedding-3-small` returns 1536-d vectors. → **`EMBEDDINGS_ENABLED` stays auto (= true)**; semantic memory and the L3 cache use pgvector `vector(1536)`. The lexical fallback remains for degraded mode.
- Live chat probe: `openai/gpt-4o-mini` is served as `gpt-4o-mini-2024-07-18`; JSON mode works. `cached_tokens` was **0 on an identical repeat**, but the prompt was only 69 tokens, below OpenAI's 1,024-token prefix-cache minimum, so this is **inconclusive, not a negative**. **Re-checked in Phase 3:** with the categorizer prompt (static prefix > 1,024 tokens), 25% of prompt tokens were reported as `cached_tokens` across a full eval. Mesh passes provider prefix caching through, so L0 caching works. At runtime, `EMBEDDINGS_ENABLED` auto-detects: if the probe fails, we use the lexical fallback (FTS + pg_trgm).
- `usage.prompt_tokens_details.cached_tokens` is always present; `0` may mean "the route doesn't report it". Mesh has its own response cache (`X-Cache: HIT`, not billed); we send `X-Mesh-Cache: no-store` on eval calls so latency/cost numbers are honest.
- `GET /v1/models` returns a bare JSON array (not OpenAI's list object), so we call it with httpx, not `client.models.list()`.

### LangGraph Redis checkpointer
- `langgraph-checkpoint-redis` 0.5.2 needs RedisJSON + RediSearch, which are built into **Redis 8**. → `finpilot-redis` uses `redis:8` (Langfuse keeps its own `redis:7`). Classes: `langgraph.checkpoint.redis.RedisSaver` (`.setup()` once), `AsyncRedisSaver`; the client needs `decode_responses=False`.

### MCP SDKs
- `mcp` 2.3.0 (official): `FastMCP` was renamed to `mcp.server.MCPServer`; the high-level client is `mcp.Client(url | server_instance)`; `streamable_http_client` replaces `streamablehttp_client`; results use `structured_content`.
- `fastmcp` 4.1.0 (PrefectHQ) is built on `mcp>=2,<3`. The FX server uses `fastmcp.FastMCP` (`@mcp.tool`, `@mcp.custom_route("/health")`, `mcp.run(transport="http", host, port)`, served at `/mcp`).
- **Client choice:** the app uses the official `mcp.Client` over streamable HTTP (as the amendment asks). Contract tests run the FastMCP server in-process on a real socket.

### FX source: Frankfurter v2
- `https://api.frankfurter.dev/v2/rates`. Key-less, no quotas (abuse rate limit), INR and USD supported (providers include ECB and FBIL). v1 has been deprecated since 2026-05 (still served). Source: https://frankfurter.dev/
- Endpoints: `/v2/rates?base=USD&quotes=INR[&date=YYYY-MM-DD | &from=..&to=..][&providers=ECB]` → `[{"date","base","quote","rate"}]`.
- Weekends/holidays: the blended default returns rows for every day. With a single provider (`providers=ECB`) a weekend request returns the previous business day's `date`. **Decision:** pin `FX_PROVIDER=ECB` for reproducible, business-day reference rates; always store the returned `date` as `fx_rate_date`; resolve a missing date to the nearest previous date ourselves too (defense in depth).
- Rates are stored as INR per 1 unit of quote currency (`rate_inr_per_unit`), so we request `base=<quote>&quotes=INR`.

### Third-party currency MCP servers evaluated (not adopted)
| Server | Source | Transport | Notes |
|---|---|---|---|
| anirbanbasu/frankfurtermcp | Frankfurter | stdio/SSE/HTTP (FastMCP) | closest fit; small project |
| cyanheads/exchange-rates-mcp-server | Frankfurter (ECB) | stdio/HTTP, hosted endpoint | TypeScript |
| wesbos/currency-conversion-mcp | Frankfurter | HTTP/SSE | demo-grade |
| lcfranca/frankfurter-forex-mcp | Frankfurter | official SDK | small |

All are small and unofficial (0–36 stars). **Decision:** we ship our own `mcp_servers/fx` (reliability, testability, no third-party trust, contract tested). `config/mcp_servers.yml` makes swapping one in a config change.

### Dev-time MCP (`.mcp.json`)
Context7 (remote HTTP, Bearer header), Postgres MCP Pro `crystaldba/postgres-mcp` with `--access-mode=restricted` (the official server-postgres is archived), `@playwright/mcp`, GitHub remote MCP (`https://api.githubcopilot.com/mcp/`, PAT header), Langfuse Docs MCP (`https://langfuse.com/api/mcp`, no auth). Secrets come only from env vars via `${VAR}` expansion.

## 1. Architecture decisions
- **Postgres-first** (Amendment A): DDL in Alembic as raw SQL; SQLAlchemy is only the access layer; analytics live in `.sql` files.
- **Package name** `finpilot` (repo dir is `finplot`).
- **Why MCP for FX (and nowhere else):** FX is an external data dependency that the chat agent also calls as a tool. An MCP server gives one allowlisted, contract-tested, swappable interface for both. Telegram/SMTP stay plain adapters (no benefit from MCP).

## 2. Engineering conventions decided in Phase 0
- **Database roles:** `finpilot` owns the schema and runs migrations. `finpilot_app` is the runtime role (NOBYPASSRLS, DML only). `finpilot_admin` runs system jobs (BYPASSRLS; every use passes a `reason`, recorded via `app.audit_reason`). `finpilot_ro` is the read-only role used by the dev-time Postgres MCP. Default privileges are granted in migration 0001.
- **Tenant context:** `user_session(user_id)` runs `set_config('app.user_id', <validated uuid>, true)`, which is equivalent to `SET LOCAL`. A test proves the value doesn't leak to the next pooled transaction.
- **LLM client:** the OpenAI SDK's own retries are disabled (`max_retries=0`). Our tenacity policy (exponential backoff with jitter) is the only retry layer, so attempts are bounded and visible. Transient failures raise `LLMUnavailable`, which callers treat as the trigger for degraded mode.
- **Inline-prompt scan:** it covers every `.py` file except `tests/`. Tests need adversarial strings such as "IGNORE PREVIOUS INSTRUCTIONS" as fixtures.
- **Lint:** `ruff format` owns line length, so E501 is disabled.
- **Langfuse SALT pitfall (hit in Phase 0):** headless init hashes the project secret key with `SALT` once, at first boot. Changing `LANGFUSE_SALT` in `.env` afterwards makes every export fail with 401. `.env.example` now warns about this, and `make langfuse-reset-keys` re-creates the key.

## 3. Phase 1: identity, tenancy, security, FX
### Multi-tenancy, in three layers
1. **Principal:** the user id comes only from the verified access token (Bearer header or `fp_access` cookie), never from a body or query parameter. Pydantic models use `extra="forbid"`, so a smuggled `id` gets a 422.
2. **Repositories:** every function takes `user_id` and filters by it.
3. **Postgres RLS:** every user-owned table has the policy `user_id = app_current_user_id()`, where `app_current_user_id()` reads the setting from `SET LOCAL app.user_id` and the value is validated as a uuid in Python. The runtime role is non-owner with NOBYPASSRLS. `v_transactions_reporting` is `security_invoker`, so RLS also applies through the view.

Tests prove that a deliberately unfiltered query sees only the caller's rows, and that cross-tenant UPDATE/DELETE statements affect 0 rows. A **meta-test fails if any table with a `user_id` column lacks RLS**, and a second meta-test fails if any non-public route answers without authentication. Both guard every future phase.

**Pre-auth lookups** (email → user; refresh/reset token hash → owner; first-user check) cannot know the tenant yet. They go through four narrow `SECURITY DEFINER` functions that return only the needed columns. EXECUTE is granted to the app role only. This keeps the bypass surface to four reviewed SQL statements instead of a privileged connection in the request path. System jobs use the `finpilot_admin` role (BYPASSRLS).

### Sessions
- **Access JWT:** HS256, 15 minutes. It carries `iat_ms` because JWT `iat` has 1-second granularity, which is too coarse for revocation. Revocation hints in Redis: `sess:rb:{user}` (revoke all tokens issued before a time; set on password change/reset and on reuse detection) and `sess:deny:{jti}` (logout). Both fail open if Redis is down; refresh tokens in Postgres remain the durable control.
- **Refresh tokens:** opaque, stored as HMAC-SHA256 (keyed with SECRET_KEY), rotated on every use, grouped into a `family_id`. Presenting an already-used token revokes the whole family and the user's live access tokens.
- **Grace window (10 s):** when parallel requests present the same just-used token, we issue a sibling token instead of treating it as theft. Without this, two simultaneous htmx requests after the access token expires would log the user out. This is a standard trade-off.
- **Browser sessions:** cookies (`HttpOnly`, `SameSite=Lax`, `Secure` in prod). Middleware transparently rotates an expired access cookie using the refresh cookie.

### CSRF
Double-submit cookie (`fp_csrf`, SameSite=Strict, readable by our JS). The token is required on unsafe methods whenever auth cookies are present, and **always for form posts**, which blocks login-CSRF. Bearer clients and cookie-less JSON clients are exempt: browsers cannot attach our cookies or send cross-site JSON without a CORS preflight, and CORS is locked to APP_BASE_URL.

### CSP
`script-src 'self' 'unsafe-eval'` plus two pinned CDNs. `'unsafe-eval'` is required by Alpine.js's standard build. There are **no inline scripts** (all JS is in `/static`), so there is no `'unsafe-inline'` for scripts. `style-src 'unsafe-inline'` is needed because the Tailwind Play CDN injects a `<style>` element. A build step (Tailwind CLI plus Alpine's CSP build) would remove both relaxations; the spec forbids a JS build step, so we accept them.

### Brute force
- Redis fixed-window limits: login 5/min/IP, register 10/h/IP, reset 3/15 min per email and 9/15 min per IP. They fail open.
- Per-account exponential lockout stored in Postgres (after 5 failures: 1, 2, 4… minutes, capped at 24 h). This control is durable even without Redis.
- Unknown emails run a dummy argon2 verify to reduce timing differences. Forgot-password always returns 202 (no enumeration).

### FX (Amendment B)
- `FXService.rate_to_inr` resolves in this order: a stored rate within 4 days before the date → one range fetch (MCP → direct HTTP) → stale stored rate (flagged stale if older than 3 days) → pinned YAML rate (flagged approximate). Every route attempt is logged in `fx_fetch_log`.
- `convert()` goes through INR at full precision and rounds once (half-even).
- The reporting view returns the **original amount** when the transaction currency equals the reporting currency, so INR→USD→INR round trips are exact.
- MCP output is validated with the same parser as the HTTP source; malformed rows fail that route and the chain continues.
- `refresh_fx_rates` runs at 02:30 and 16:30 UTC (ECB publishes around 16:00 CET), and at worker start if rates are stale.

## 4. Phase 2: ledger, recurring, imports
- **`data_version` is maintained by the database.** Statement-level AFTER triggers with transition tables bump `users.data_version` once per statement per affected user, in the same transaction as the write. A 50k-row import is one bump; a rollback undoes the bump; no service code can forget it. The reporting-currency change bumps through a BEFORE UPDATE trigger. Redis `dv:{user}` is only a mirror: `user_session` compares the value before and after and writes it after commit. Readers fall back to Postgres on a miss.
- **Duplicates.** `dedupe_key` = hash(date, signed amount, currency, normalized description, account). `hash` = dedupe_key plus an occurrence suffix (`i:<n>` for the n-th identical row in a file, `m:<uuid>` for manual entries, `r:<rule>:<date>` for recurring). Consequences: two identical coffees in one file both import; re-uploading the same file marks every row as a duplicate; a bank row matching a manual entry is flagged in the preview. The user can choose to import duplicates anyway.
- **Recurring math is anchor-based.** Occurrence k is computed from the aligned start date, not from the previous occurrence, so month-end clamping never drifts (31st → Feb 29 → Mar 31). Editing a rule changes future occurrences only. Occurrences that fall while a rule is paused are not created. If an occurrence fails (for example FX is down), the materializer stops at that date and the next tick retries from there, so nothing is lost. Idempotency comes from the partial unique index `(recurring_rule_id, date)` plus `ON CONFLICT DO NOTHING`.
- **Category decisions are deterministic and ordered:** explicit user choice → the user's merchant memory → the user's keywords (0.9) → (Phase 3) LLM. Recategorization previews never touch rows whose `cat_source = 'user'`.
- **System categories** are rows with `user_id IS NULL`. RLS allows everyone to read them, and only owners can write their own rows. They are hidden per user through `hidden_categories` rather than deleted.
- **Import pipeline.** The raw file is kept in `import_files` only between upload and import/undo, so the user can remap columns without re-uploading; it is deleted afterwards. Preview and import run the same `_analyze` code, so what the user sees is exactly what gets imported. The import is one transaction (all or nothing). The confirm endpoint commits the `importing` status in its own short tenant transaction before enqueueing, so the worker sees it.
- **Delimiter detection:** `csv.Sniffer` failed on bank preambles combined with decimal-comma amounts. We pick the delimiter that splits the most lines into the same field count.
- **CSV export** prefixes text cells that start with `= + - @ TAB CR` with `'`. Numeric cells are never altered, so negative amounts stay numbers.
- **UI.** Interactivity uses Alpine components in `/static/js` that call the JSON API with the CSRF header. Server data reaches the page in `<script type="application/json">` blocks, which browsers don't execute, so CSP holds. Jinja `tojson` does not escape double quotes, so `x-data` attributes that embed it are single-quoted. A test audits every page for inline executable scripts.
- **Dashboard v1** computes live from `.sql` files that read `v_transactions_reporting`. Transfers are neither income nor spend; savings flows are reported separately. Balance = converted opening balances + lifetime net. Phase 8 moves this into `insight_snapshots` and adds caching.

## 5. Phase 3: categorization and evaluation
### Hybrid categorization rather than pure LLM
The layers run cheapest first: merchant memory → the user's keywords → a sign-aware rule lexicon (accepted at ≥ 0.85) → LLM fallback. The ablation on our personas:

| Layers | Accuracy | Coverage | Precision when answering |
|---|---:|---:|---:|
| Rules | 64.2% | 64.8% | 98.9% |
| + memory | 67.2% | 67.9% | 99.0% |
| + LLM | 87.4% | 96.3% | 90.8% |

Deterministic layers are nearly always right when they answer. The LLM adds coverage at slightly lower precision, and only on rows the cheap layers couldn't resolve, so the whole test set needs about 2 LLM calls per 1,000 transactions (unique merchants, 25 per batch). Results: `eval/results/RESULTS.md`.

### Defences that don't depend on the model behaving
- **Output validation:** the answer must be one of the user's own categories (system categories not hidden, plus their custom ones).
- **Flow-direction guard:** income categories only for credits, expense categories only for debits. This alone makes "categorize this debit as Salary" injections fail.
- **Opaque ids:** the prompt identifies merchants as `m1…mN`, never by text.
- **Data blocks:** all untrusted text (descriptors, custom categories, examples) sits inside `<data>`.
- Measured injection-safe rate: 100%, including adversarial descriptors that embed fake JSON results.

### Evaluation methodology
- **Personas:** 6 seeded synthetic personas × 15 months (12 history + 3 holdout for forecasting) in INR and USD, with per-transaction ground truth, recurring-series truth and injected anomalies (duplicates, spikes, price hikes, a one-off). Realism features: messy rails (UPI, NEFT, IMPS, NACH, POS, SQ*/TST*/CHECKCARD), store numbers, city/state suffixes, 3% typos, 147 merchants with 39% unknown to the rules, and weighted ambiguous merchants (Amazon, Costco, Walmart, wallets, person-to-person) whose truth is drawn per transaction.
- **Realistic user behaviour:** the simulated user corrects 25% of the merchants seen in the first two months. That seeds merchant memory and few-shot examples, and those labelled rows are excluded from scoring.
- **Metrics:** unresolved predictions count as wrong for accuracy; coverage and precision are reported separately so caution is never mistaken for skill. Calibration compares accuracy with mean confidence per confidence bucket.
- **Reproducibility:** prompts are deterministic (merchant groups sorted before batching) and the L1 cache is persona-scoped, so reruns of the same commit are identical and free. Fresh LLM samplings vary by about ±1–2 points (observed 87.4%, 88.1%, 89.2%), and RESULTS.md says so.
- **Synthetic users:** `is_synthetic`, cannot log in, excluded from the first-admin rule, and deleted after every run. Synthetic FX rates live in `fx_rates` with `scope='synthetic'` and are only ever joined for synthetic users.
- **Gates** live in `config/thresholds.yml`. Offline gates protect the deterministic layers (precision when answering ≥ 0.90, coverage ≥ 0.45, injection-safe = 1). Full gates protect the whole pipeline (accuracy ≥ 0.80, macro-F1 ≥ 0.70, unseen ≥ 0.65, custom-category recall ≥ 0.70). They were set from the first real runs.
- **Langfuse:** one dataset per suite. Each eval run creates an experiment run named `<suite>/<git_sha>/<timestamp>`; items replay the stored prediction (deterministic, free), carry `correct` scores plus a run-level accuracy, and link back to `eval_cases.langfuse_trace_id`. The categorizer's own batched LLM calls are traced with `session_id = eval:<run>:<persona>`. Up to 300 cases per suite are mirrored (all failures first); Postgres keeps all of them.
- **Honest weak spots** (from the insights panel): education/daycare, impulse/misc versus groceries for kiranas, "other" versus transfers for person-to-person payments, irregular freelance income from unknown payers, and ambiguous marketplaces (39%).

### Commit before response (bug found in Phase 3)
FastAPI ends `yield` dependencies after the response is sent by default. Our `get_db` commits in its teardown, so a client could receive 201 and then read stale data: the e2e "Load sample data" flow showed it, and a live probe measured 1/40 stale reads. Every route now declares `Depends(get_db, scope="function")`, which commits before the response is sent. A meta-test walks all routes, including lazily included routers, and fails on any other usage. After the fix: 0/200 stale reads.

### Sample data and demo accounts
Until §15, "Load sample data" reused the eval generator (a persona in the user's currency, the last 6 months shifted by whole months so day-of-month patterns line up with the declared recurring items). Since §15 it loads committed files from `data/sample/`; the generator-based loader stays for the cache and memory eval suites (`eval/synth/persona_sample.py`). The admin rule is "the first real registrant while no admin exists", so demo accounts never take the admin role by registering.

## 6. Phase 4: recurring detection, analyzer, anomaly and recurring suites
### Derived tables never bump `data_version`
`detected_recurrences`, `analysis_findings` and `insight_snapshots` are rewritten by every analyzer run, so they have no data_version triggers. Otherwise every analysis would invalidate every cache. User actions on them (dismiss, usage rating, promote) delete the user's analysis snapshot instead. The analyzer also never writes `transactions` (the spec's `transactions.detected_recurrence_id` exists with an FK but stays unset): evidence lives in `detected_recurrences.txn_ids_json`.

### Snapshot and concurrency
`get_report` serves `insight_snapshots(kind='analysis')` while `(data_version, reporting_currency, local date)` match. Otherwise it re-runs under `pg_advisory_xact_lock(hashtext('analysis:<user>'))`, so concurrent page loads compute once (the second waits, then finds the fresh snapshot). A full run takes about 100–250 ms for 15 months of data. Celery `refresh_analysis` pre-warms after background categorization.

### Recurring detection
- **Grouping:** by (merchant_norm, direction, original currency). A $20 subscription is a fixed series even when its INR cost moves.
- **Same-payee spellings are merged:** identical letters per word catches adjacent swaps (SPTOIFY), or a difflib ratio ≥ 0.88. Singleton spellings only join a larger group. The SQL returns single charges for this reason.
- **Period fitting:** every period from weekly to yearly is tried. A chain keeps charges at least half a period apart; of two close charges it keeps the one nearer the series level, so duplicates don't break a series.
- **Acceptance:** at least 70% of gaps fit within ±tolerance of 1 period (or 2 periods, one missed charge); at least 3 charges (2 for yearly); amount CV ≤ 0.35 (≤ 0.10 for yearly); and the merchant's other charges are at most a third of the series. Without that last rule, a coffee habit hides a "quarterly" sub-chain. The best score (regularity × share of charges kept) wins. Groups with ≥ 6 charges that fail as a whole are retried per amount cluster (one merchant, several subscriptions).
- **Monthly tolerance is ±6 days.** A property test showed that ±4 rejects real series: a bill due on the 1st that posts 3 days late in February leaves a 25-day gap. Cost: recurring precision on the eval went from 98.2% to 96.4% (one more false positive).
- **Next date:** stepped from the last charge's *due* date (a bill due on the 31st that posted on the 3rd is due again on the 31st). It is validated against the period and otherwise falls back to the median recent gap. Also found by the property test.
- **Declared rules override detected series:** a rule links to at most one series, by recurring_rule_id, by the same merchant name, or by the same period with amount within 10% and a compatible category. Linked series are never suggested. "Make it recurring" creates a rule whose first date is strictly after today, so the materializer never back-fills charges that already exist as real transactions.

### Anomalies: what each one states
Every finding has metric, baseline, observed, threshold, unit and evidence transaction ids. Text is rendered from `templates/messages/findings.yml` (sandboxed Jinja, `money()` for ₹/$ formatting). An LLM narrator can come later (Phase 6, behind the grounding guard), but numbers never come from it.
- **Outliers:** robust z (Iglewicz–Hoaglin, 0.6745·(x−median)/MAD) on log amounts with a leave-one-out baseline. The merchant baseline is used with ≥ 4 other charges, else the category baseline with ≥ 8. Thresholds: z ≥ 3.5 **and** ≥ 3× the median **and** ≥ the currency's `min_anomaly_amount`. Members of detected series are excluded (price-hike logic covers them), and a category outlier is skipped if it is normal for its own merchant.
- **Duplicates:** same merchant + same original amount within 3 days, above the small-purchase ceiling. Fixed-price merchants that often charge within the window (tuition, daycare) are exempt.
- **First-time large:** first-ever charge at the merchant, at least max(4 × floor, 97th percentile of variable spend), not in the first 60 days of history, and nothing at least half that size before in the same category.
- **Price hikes:** change points on fixed-amount series (prior CV ≤ 0.05 over ≥ 3 charges) where the new level persists. Rises ≥ 5% are hikes, priced per year in the reporting currency.
- **Category spikes:** robust z on monthly totals versus the previous ≤ 6 months (≥ 3 required). Skipped when the baseline median is 0 or the category is Uncategorized.

**Tuning disclosure:** `min_ratio` was 2.0 in the first run (precision 37.5%, F1 0.52, 0.43 false alarms per clean month). After inspecting the false positives on these same personas it was raised to 3.0, and four other fixes went in alongside (order codes stripped by the normalizer, first-time warm-up, series members excluded from outliers, routine fixed-price merchants exempt from duplicates). Result: precision 56%, F1 0.68, 0.20 false alarms per month, with recall unchanged at 86%. Because the change was chosen on the evaluation data, anomaly precision is optimistic until measured on held-out personas. RESULTS.md says so.

### Subscriptions and "unused"
A subscription is a detected expense series in the `subscriptions`/`entertainment` categories, or a fixed-price (CV ≤ 0.10) monthly/quarterly/yearly charge in a discretionary category. Train tickets every two weeks are not a subscription. Flags:
- **`price_hike`**
- **`overlap`:** ≥ 2 active services in one group of `config/subscriptions.yml`, matched by merchant-name regex.
- **`low_value_proxy`:** the user rated usage 0–1 of 5 ("you rated it rarely used"), or it is unrated and costs ≥ 2× the median subscription ("likely low value (heuristic)").

"Unused" is never asserted. A test checks that no finding text contains the word.

### `fx_exposure` (Amendment 1, P1)
- **Share of spend:** the share of the last 90 days' spend in currencies other than the reporting currency.
- **Per foreign recurring series:** `cost_last − cost_first = (orig_last − orig_first) × rate_first + orig_last × (rate_last − rate_first)`. That is the price effect plus the FX effect, exact by construction (the FX effect is computed as the remainder after rounding). Rates are the ones stored with each transaction; nothing is fetched or invented.
- **Test:** $20 → $22 at 83.00 → 85.50 gives price +₹166.00 and FX +₹55.00.

### Patterns (no time-of-day in bank data)
Weekend vs weekday daily discretionary spend; spend in the 3 days after a payday (an income credit ≥ 25% of median monthly income) vs a typical 3-day window; impulse-category frequency (an explicit proxy, since there are no times of day); top discretionary category share; savings-rate trend (first vs last 3 of 6 full months); "latte factor" via the per-currency small-purchase ceiling. All metrics appear in the report even when no threshold is crossed.

### Evaluation (suites 3 and 4)
- **Anomalies:** ground-truth categories, as_of = last history day, full-history scan. Matching is type-aware (spike ↔ merchant/category outlier, one-off ↔ first-time large or outlier). Recall is reported on *material* anomalies (above the product's floor: a 5× spike on a ₹60 chai is out of scope by design) and on all anomalies. False alarms per month are measured on clean copies of every persona (same seed, nothing injected).
- **Recurring:** majority-overlap matching to truth series; eligibility is ≥ 3 history charges (≥ 2 yearly). Amount error is measured against the series' nominal price after any injected hike: the first version compared with the median of the last 3 charges, which is circular and scored 0. Next-date error uses the first held-out charge. The generator now posts bank debits due on weekends on Monday, so date error is not trivially zero.
- **Materializer:** three INR rules (monthly on the 31st, biweekly, quarterly) starting 120 days back, with ticks at −60 days, again, today, and again. Expected dates come from an independent enumerator in the suite.
- **Gates** (offline and full): anomalies recall ≥ 0.75, precision ≥ 0.45, ≤ 0.35 false alarms per month; recurring precision/recall ≥ 0.90, period accuracy ≥ 0.95, next date within 3 days ≥ 0.90; materializer correct and idempotent = 1.0.
- **Regression insights:** compare only runs with the same suites and personas, and know which metrics are lower-is-better.

## 7. Phase 5: forecaster, simulator, natural-language what-if
### Model (`finpilot/forecasting/model.py`, pure numpy)
- **Variable spend:** for each future day, every path samples a historical day with the same weekday and takes that day's flows for all categories. This keeps day-of-week seasonality and cross-category correlation without fitting a parametric model. Recent behaviour weighs more through a per-category ratio (exponentially weighted mean ÷ plain mean, half-life 45 days).
- **Payday effect:** a multiplier for discretionary streams in the 3 days after projected paydays, estimated from history and clipped to [0.7, 2]. It is compensated on the other days so that *expected* spend is unchanged; a test enforces that exactly.
- **Scheduled items:** declared rules first, then active detected series not linked to a rule (declared wins). Bills that never posted on a weekend move to Monday. Amounts get a mean-preserving lognormal with the series' CV.
- **FX (P1):** foreign-currency items get a random walk with daily σ from stored rate log-returns.
- **Parameter uncertainty:** each path also rescales its variable spend by a bootstrap re-estimate of the level from the 180-day window. Without it the bands were overconfident: daily coverage was 72% and fell to 62% at 90 days. With it: 77% daily and 79% at 90 days, target 80%. This was chosen after seeing the backtest, but it is a standard method rather than a threshold. Both numbers are recorded here.
- **Common random numbers:** variable spend uses one seeded stream. Each scheduled item draws from `SeedSequence([seed, hash(item key)])`, keyed by stable series keys rather than database ids. So a scenario that adds or removes items changes nothing else in any path. That is what makes "delta = purchase" exact path by path, and what makes lever comparisons fair.
- **Sparse data** (< 2 months): flagged low-confidence and the band is widened 1.5× around the median. Never an error.
- **Naive baselines** for evaluation: the trailing 90-day mean daily net flow, and last month replayed by day of month.

### Simulator
- **Scenarios** are a pydantic discriminated union: purchase, recurring cost, cancel subscription, income change, savings transfer, category cut, loan/installments (standard amortization), and composites.
- **Foreign-currency amounts** are converted at the latest **stored** rate, and the rate, date and source are returned and stated in the answer.
- **Verdict rule:** affordable if p10 ≥ cushion for the whole horizon; risky if p50 ≥ cushion but p10 dips; otherwise not affordable. The cushion is `user_settings.cash_buffer`, else the currency default.
- **Levers** (delay 30/60 days, 3 interest-free installments, −20% on the top discretionary category, cancel the priciest subscription) are each re-simulated with the same seed. Only levers that verifiably raise the bad-case low are returned, best two first.

### Natural-language what-if (LangGraph + Redis)
- **Flow:** the fast LLM parses to `ParsedScenario`. Code (not the model) decides which slots are missing. A missing slot triggers a clarifying question from `templates/messages/simulation.yml` via `interrupt()`; the state is checkpointed with `langgraph-checkpoint-redis` (`RedisSaver`, 60-minute TTL), and `POST /simulate/ask/{thread}/resume` continues with `Command(resume=…)`. Then the deterministic simulator runs and a templated answer is returned. LLM wording behind the grounding guard comes in Phase 6.
- **Re-execution:** a resumed node re-runs from its start, and the repeated parse call is served by the L1 cache.
- **Tenancy:** thread ids are `sim-<user_id>-<random>`, and resume checks the prefix.
- **Redis Search only allows indexes on DB 0** ("Cannot create index on db != 0"), so checkpoints always use DB 0 of the configured Redis. Tests are isolated by key prefix (`LANGGRAPH_KEY_PREFIX=lgtest`, which also gives a separate index), not by DB number. An early scratch test passed on DB 15 only because the index already existed from DB 0.
- **Parser prompt v2:** v1 called "can I afford a trip to Bali?" (no amount) *unsupported* instead of asking for the amount, and returned −30 for "cut dining by 30%". v2 adds two explicit rules, and v1 is kept. The 100% NL score is on the same 18-question set that motivated the change, so it is optimistic.

### Persistence and caching
`forecast_runs` stores every baseline and scenario result (kind, data_version, seed, scenario JSON, result JSON) so Phase 6 recommendations and the decision trail can cite a forecast by id. `get_forecast` reuses the latest baseline while data_version, currency, date and horizon match, and otherwise recomputes under a per-user advisory lock. `goals` has data_version triggers: goals are financial facts per the spec. A forecast takes about 90 ms (2,000 paths × 90 days); a simulation with levers takes about 100–300 ms.

### Evaluation (suites 2 and 5)
- **Forecast backtest:** ground-truth categories; 4 rolling origins (end of history months 9–12) × 6 personas × 90 days. Each origin sees only data up to that day, with detection re-run as of the origin. Error at days 30/60/90 is normalized by the persona's monthly spend. Coverage is reported per horizon and per day, plus skill vs both naive baselines and a sparse-data check. Gates: daily coverage in [0.65, 0.95] (too-wide bands also fail), skill ≥ 0.30 vs trailing mean and ≥ 0.20 vs last month.
- **Simulation:** 282 property checks on real persona data (purchase never raises any path; delta = purchase; cancel = charge × occurrences in every path; same seed identical; composite = sum of parts) must all hold. The historical check starts just before each persona's real one-off purchase: the baseline band contains reality on 0% of days afterwards, the scenario band on about 59%, and the scenario is closer for both personas. NL parsing is scored in full runs.
- **Seeds** in evals are persona-based (synthetic users are re-created every run), so reruns of a commit are identical.

### Sample data
The persona sample (the product's sample data until §15) leaves out credit-card bill payments: the persona also lists the card purchases one by one, so a single-account demo counted that spending twice and forecast a 97% chance of going below zero.

## 8. Phase 6: advisor, grounding guard, decision trail
### Grounding guard (`finpilot/trail/grounding.py`)
- **Extraction:** every amount, percentage, number and date in LLM text: ₹ / Rs / INR / $ / USD as prefix or suffix, Indian and Western grouping, "1.5 lakh", "2 crore", "80k", signs, "23.1%", "5.3×", ISO and written dates, and "March 2026". Each character is consumed once (dates before amounts before plain numbers).
- **Currency-aware matching:** "₹1,881" passes only if an INR fact near 1,881 exists, and "$1,881" fails against the same fact. The model therefore cannot convert currencies; a converted figure is accepted only when code put it in the facts. Foreign-currency candidates carry the rate and rate date as facts (Amendment).
- **Tolerance:** max(1% of the fact, the currency's `grounding_abs_tolerance`, half a unit of the precision the text was written with), so "₹79,000" ≈ ₹78,947.53 and "5×" ≈ 5.3×. Signs are ignored ("saves ₹X" against a negative delta).
- **Names are not claims:** digits inside known names are masked before checking ("LYFT RIDE SAT 11PM").
- **Scoped facts:** each recommendation is checked against **its own** candidate's facts plus the findings it cites and forecast basics, not against every candidate's facts. With pooled facts, a corrupted "$237" passed because an unrelated candidate had a fact near $237.
- **`guarded_json`:** generate → check (+ caller validation) → regenerate with the problems named (`templates/messages/grounding.yml`; at most 2 retries) → deterministic fallback. It returns `{verified, unverified, attempts, retries, first_attempt_pass, fallback_used, problems, model}` and emits the Langfuse scores `grounding_pass` and `grounded_number_ratio`. Retry turns are part of the L1 cache key, so evals stay reproducible; schema-repair turns are never cached.
- **Known limit:** a number matching an unrelated fact of the same candidate within 1% passes. In the eval, 1 of 40 deliberate corruptions slipped through this way: a scaled −₹6,190 → −₹8,480 matched another fact. It is a number-matching guard, not a semantic one.

### Advisor
- **Candidates are deterministic** (`finpilot/advice/candidates.py`): cancel a flagged subscription, cap a category that spiked or trends up or dominates, close a forecast shortfall with the smallest cut that verifiably helps, catch up on a savings goal, respect a spending cap, delay a risky planned purchase (the latest what-if), stop paying fees, ask for a refund of a duplicate charge, and declare a confident detected repeat.
- **What each candidate carries:** est_monthly_impact, est_total_impact, finding ids, evidence txn ids, named entities, and a same-seed simulation of its effect (expected end-balance change, bad-case low, chance of dipping below the cushion).
- **Ranking:** monthly impact ÷ monthly spend + 2 × risk relief + a severity bonus.
- **The advisor owns three finding types** (kinds `forecast` and `goal`, never deactivated by the analyzer): forecast_shortfall, goal_gap, planned_purchase_risk. The analyzer added `fees_paid` and `recurring_not_declared`. Together these mean every candidate cites ≥ 1 finding.
- **What the smart model sees:** opaque ids (c1…, f1…), names and facts only, inside `<data>`. It picks the top 3.
- **Validator:** known candidate ids; exactly min(3, n) distinct; ≥ 1 cited finding of that candidate; names one of its entities; every number is a fact of that candidate. Failures are retried with the problems named, then fall back to `templates/messages/advice.yml`. Without Mesh, the templates are used directly.
- **Dismissals are remembered** by candidate key for 90 days, and their reasons are passed to the prompt (long-term memory proper arrives in Phase 9). A dismissal on a card that a background refresh had just superseded still counts, and applies to the same advice in the newest run. That race was found by e2e.
- **Decision trail:** trigger, cited finding ids, evidence ids, forecast_runs id + baseline outcome + scenario + simulation, the candidate snapshot, reasoning, model, prompt name/version (or the template file), grounding report (run-level and per recommendation), and the Langfuse trace id from a per-run `agent_span`.
- **When it runs:** `refresh_advice` (Celery, once per data_version) after `refresh_analysis`, or on demand from the Advice page.

### Narration behind the guard
- **What-if answers:** `simulator_parser/verbalize` writes the answer from the simulation facts, with the template as fallback.
- **Insights summary:** `analyzer_narrator/summary` writes 2–4 sentences over the top findings, cached per data_version (`insight_snapshots` kind `analysis_summary`, cleared together with the analysis snapshot).
- **Formatting rule:** both prompts require amounts with currency symbol and grouping. The first live answer wrote "−88389", which passed the guard but read badly.

### Evaluation (suite 6)
- **Setup:** per persona: as_of = last history day, ground-truth categories, and an opening balance so the history never goes below zero plus one month of income. Personas don't define balances; with zero, every one of them looked permanently overdrawn and half got the same "cut Shopping 50%" advice.
- **Per recommendation:** cited, specific, grounded (scoped facts).
- **Guard robustness:** 40 corruptions of real recommendation texts (an amount ×1.37, a fabricated sentence, ₹ swapped for $). The guard must flag them, and clean texts must pass.
- **Full mode adds:** first-attempt grounding pass rate, fallback rate, and LLM-as-judge on the committed rubric `prompts/eval_judge/advice.v1.md` (smart model; specificity, actionability, grounded, relevance, overall 1–5).
- **Distinctness:** the share of personas whose top recommendation is theirs alone.
- **Memory:** dismiss the top recommendation, re-run, and it must not come back.
- **Subscription heuristic tightened** after the first full run: a monthly remittance to parents in the system "Other" category was offered as "Cancel PARENTS subscription". Outside the subscription categories, only the user's own custom discretionary categories qualify now.

## 9. Phase 7: coordinator, pipeline trace, goal tracker, degraded mode
### Graph (`finpilot/agents/coordinator.py`)
```
route_intent ─┬─ refresh_insights → ingest → [categorize] → analyze → forecast → goals → advise
              │                       → persist_trail → snapshot → [deliver] → END
              ├─ ask_affordability → NL what-if (its own interruptible simulator graph)
              └─ explain → decision-trail lookup
```
- **Checkpointing:** state lives in the LangGraph Redis checkpointer (DB 0, `LANGGRAPH_KEY_PREFIX`) under thread `user:{id}:run:{run_id}`, saved after every node.
- **Every node commits its own DB transaction.** So "checkpointed after node X" really means X's writes are durable, and `resume_run` (`graph.invoke(None, …)`) re-executes only the failed node and what follows. Test: an injected crash in `forecast` → resume → ingest/analyze ran exactly once, and forecast shows `failed` then `succeeded`.
- **Conditional edges:** categorize is skipped when nothing is unresolved; deliver runs only when a digest is due (Phase 10). Routers have no side effects. The node *before* a skip writes the reason into `state.plan`, and `_finish` records `skipped` rows, so the trace shows every step.
- **Trace:** each node writes an `agent_runs` row (status running → succeeded / degraded / failed / skipped, summary JSON with timings), updates Redis `run:{run_id}` (24 h TTL; Postgres is the durable record), and opens a Langfuse span inside the run's `agent_span` trace. The Langfuse score `degraded` is set per run. Manual spans were used instead of the LangChain callback handler, because nodes call our own instrumented clients.
- **Degraded mode:** Mesh not configured at start, or any LLM failure during the run, sets `state.degraded`. From then on every node gets `llm=None`: rules-only categorization, template advice and template narration.
  - A test with a backend that always fails asserts exactly one Mesh call per run: the categorizer's first batch.
  - The test found a real bug. The categorizer treated `LLMUnavailable` as a per-batch schema failure (it is a subclass of `LLMError`), so it kept calling Mesh for every batch and the run was only degraded later, at the advisor. It now re-raises and drops to rules at once.
- **Intents:** `explain` and `ask_affordability` go through the same graph via `route_intent`, but return directly. `answer_from_cache` and `free_chat` arrive with Phases 8 and 9.

### Scheduling
- **`run_pipeline`:** a Celery task with a per-user Redis lock. A created run that cannot start is finished with a reason, never left `running`.
- **`schedule_pipeline` debounces bursts of writes:** a Redis NX key plus a 90-second countdown, so a run of edits triggers one refresh.
- **Imports trigger a run directly** (the pipeline categorizes first); manual entries trigger categorization, then a debounced run.
- **`resume_stale_runs`** runs on worker start: runs still `running` 15+ minutes after starting belonged to a dead worker and are resumed from their checkpoint, or finished as abandoned when there is none.
- This replaced the Phase 4–6 chain (categorize → refresh_analysis → refresh_advice).

### Goal tracker (`finpilot/agents/goal_tracker.py`)
- **Snapshots** are keyed by (goal, ISO week). Every run in a week updates that week's row, so the history has one point per week, and the delta is always "since last week" (progress change, ETA shift, status change).
- **Behind schedule:** `goal_gap` → catch-up candidates (savings transfer, cap, and, new, `debt_catch_up` for debts).
- **Ahead:** a savings goal finishing ≥ 30 days early with a monthly surplus yields a `goal_ahead` finding and a `goal_surplus` candidate ("move the extra … to another goal or investments").
- **`debt_payoff` goals:** target = the debt, saved_amount = repaid so far. The pace is the expected monthly outflow in the loan's category (scheduled EMIs + usual payments), which gives the ETA, the required monthly amount and on_track.

### UI
- **Pipeline Trace page:** run list, a node timeline in pipeline order with status, duration, summary and skip reasons, a degraded badge with its reason, Resume for failed runs, live progress over SSE (`GET /pipeline/runs/{id}/events`, polling fallback via `GET /pipeline/runs/{id}`), and a Langfuse link for admins.
- **Dashboard:** a notice when the last run was degraded.
- **Goals:** debt payoff, plus "since last week" deltas.
- **Navigation:** with 10 sections, the full nav shows from 1280 px, with a menu below.

## 10. Phase 8: answer caching (L0–L3), intent router, pre-warming

### Flow (`finpilot/answers/service.py`)
```
question ─► intent router ─► (intent, period) ─► L2 exact ─► L3 semantic ─► compute (single-flight) ─► store
chip     ──────────────────► (intent, period) ─┘
```
- **Router (`finpilot/agents/intent_router.py`, `config/intents.yml`), cheapest layer first:**
  - Regex rules, in file order.
  - Cosine similarity to each intent's example phrasings, threshold 0.80. Example vectors are cached in Redis per embedding model and per hash of the example list.
  - Fast-LLM classification (`prompts/intent_router/classify.v1.md`), accepted only for a known intent with confidence ≥ 0.5.
  - Otherwise `free_form`.
  - The period slot comes from regexes (last month, 3/6/12 months, 30 days, this month); the default is `this_month`.
- **Handlers:**
  - `deterministic_template`: top_categories, upcoming_bills, subscription_review, goal_status, savings_rate, income_summary. Facts → `templates/messages/answers.yml`. No LLM call.
  - `llm_from_facts`: spending_trend, where_to_save, where_overspending; also `free_form` with a general fact pack. Facts → `prompts/answers/answer.v1.md` (fast model) → grounding guard → template on failure.
  - `simulator`: afford_purchase. Goes to the Phase 5 NL what-if; never cached, because the user supplies the amount.
- **Facts (`finpilot/answers/facts.py`)** come only from deterministic sources: dashboard aggregates (a `last_month` window and previous-period KPIs were added), the analysis report, the forecast and recommendations. Amounts are in the reporting currency.

### Cache layers
- **L0:** provider prefix caching. The new prompts are STATIC-first, like all others; `cached_tokens` is recorded.
- **L1:** `cache:llm:` (Phase 3). The router's LLM call is scoped per user.
- **L2 key:** `cache:ans:{user}:{intent}:{period}:{data_version}:{currency}:{locale}:{version}:{model}[:{day}][:fx{asof}]`.
  - `version` is the prompt name+version for LLM intents, or a hash of `answers.yml` for template intents, so editing wording invalidates.
  - `model` is the fast model, or `none` for templates.
  - `day` is set for time-relative intents: "this month" changes at midnight without any write.
  - `fx{asof}` is set only when the user has transactions in a currency other than the reporting one. It is the newest stored real rate date for the currencies involved, so a new rate invalidates converted totals.
  - Invalidation is purely by version (no key scanning). Old keys expire by TTL (per intent in `intents.yml`).
- **L3 (`semantic_cache`, migration 0010):**
  - pgvector HNSW cosine index plus RLS. A hit requires the same user, intent, period, data_version, currency and day bucket, and cosine ≥ 0.92.
  - Only typed questions use L3; chips have an exact intent. `free_form` uses only L3, because the same intent covers different questions.
  - L3 is disabled when embeddings are off.
- **Not cached:**
  - Degraded answers (Mesh down → standard wording), so the first answer after recovery is AI-written.
  - afford_purchase.
  - Answers whose LLM text failed grounding *are* cached. Their fallback template is correct, and recomputing would only repeat the failure until the data changes.
- **Single-flight:** `SET NX cache:lock:{hash(key)}` with a 60 s expiry. Concurrent misses wait up to 8 s for the winner's L2 value, then compute themselves. Test: 3 threads → 1 compute.

### Pre-warming
- **Pipeline:** the snapshot node calls `prewarm()` for the four dashboard chips (spending_trend, where_to_save, where_overspending, upcoming_bills). It fills L2 and stores `insight_snapshots(kind='answers')` stamped with data_version, currency and date.
- **Login:** `prewarm_answers` (Celery) recomputes when that snapshot is stale (another data_version, another currency, or not today). Pre-warm calls are not written to `answer_log`.

### Observability
- **`answer_log`** (identity bigint, RLS) records intent, period, route, cache_layer (miss | L2 | L3 | none), generated_by, latency_ms and degraded. `GET /answers/stats` gives p50/p95 by layer.
- **Langfuse:** score `cache_hit` (1/0) on the trace.
- **UI:** the dashboard badge reads "Instant answer · updated 2 h ago" or "Computed now (N ms)", with a Refresh link (`refresh=true` bypasses L2/L3).

### Eval suite 7 (`eval/suites/cache.py`, `eval/datasets/cache_questions.yml`)
- **Setup:** a synthetic user with the persona sample (`eval/synth/persona_sample.py`, what "Load sample data" loaded until §15), pre-warmed like the pipeline does.
- **Session:** 200 seeded questions (intent weights; 40% of chip-intent questions are asked as chips).
- **Invalidation:** two expenses are written at questions 70 and 140. The first answer per intent after each write must be a miss.
- **Isolation:** a second user asks the first 60 questions. Its first ask of each intent must be a miss.
- **Router:** accuracy is measured on all 35 labelled phrasings.
- **First runs:**
  - **Offline (rules only, no Mesh):** cached p95 2 ms, router 0.91. LLM intents are degraded and therefore uncached, so the hit rate is 0.16 and not meaningful offline (not gated).
  - **Full (committed run, `ff4a945`):** hit rate 0.875 (172 L2, 3 L3, 25 misses); L2 p50/p95 2/5 ms, L3 823/1809 ms (embedding call), miss p50/p95 1.1/5.6 s; invalidation and isolation 100%; router 34/35 (32 rules, 1 embedding, 1 LLM, 1 default). An earlier full run of the same code gave the same hit rate and router result, with slightly different latencies.
- **Honest caveats:**
  - The rule patterns and the eval phrasings were written together by the same author, so rule accuracy is optimistic.
  - The remaining misses were left as they are, not tuned away:
    - offline: "how am i doing on spending", "Has my spending changed compared to last month?" and "What bills are due next week?";
    - full: "how am i doing on spending" went to the default.
  - The L3 share is small because most typed phrasings match a rule and go to L2 first.
  - Latency is measured in-process (no HTTP). Cost saved = LLM-intent hits × the mean cost of an uncached answer call.

## 11. Phase 9: memory (short and long term), chat agent, memory page

### Long-term memory (`finpilot/memory/`, migration 0011)
- **Postgres + pgvector, not a vector DB:** one fewer service, writes are transactional with the user's data, RLS plus a user_id filter make per-user search trivial at this scale (hundreds of memories per user), and backup/delete-by-user come for free. A `SemanticMemory` protocol (`long_term.py`) is the seam for a Qdrant adapter if scale ever demands one.
- **Table `memories`:** kind (fact | preference | goal_context | decision | episode), **topic** (a short slot name), text, `embedding vector(1536)` (NULL without embeddings), a generated English `tsvector`, importance, confidence, pinned, source (chat | recommendation | user | eval), expires_at, superseded_by. HNSW, GIN(tsv) and trigram indexes; RLS. No data_version triggers: a memory never changes the numbers.
- **Write rules (deterministic, after extraction):**
  - Blocked content is never stored (`config/memory.yml`: long digit runs, card numbers, IFSC, "password/PIN/OTP/CVV is …", API keys).
  - Same kind + topic as an active memory: near-identical (cosine > 0.9, trigram ≥ 0.8 without vectors, or same normalized text) → merge (newer wording; importance and confidence never drop); otherwise → **supersede** (the old row points to the new one and is no longer recalled, but stays visible as history on the Memory page).
  - Otherwise an active memory with cosine > 0.9 → merge; else insert.
  - Expired memories (`expires_at` past) are not recalled.
- **Why topics:** "contradiction" detection by an LLM judging pairs is expensive and unpredictable. The writer prompt is shown the user's existing kind/topic list and reuses a topic when a statement updates one. Code then makes the supersede decision, so it is testable offline.
- **Deleting a memory also deletes the older versions it superseded.** Otherwise `ON DELETE SET NULL` would bring a contradicted memory back. Found while writing the deletion test.
- **Retrieval (`retrieval.py`):** score = 0.6·similarity + 0.25·recency (half-life 30 days from last use or update) + 0.15·importance. Pinned memories come first. Then matches above the floor (cosine 0.25, or lexical `ts_rank_cd` 0.05) up to top-5 and a 400-token budget. Recalled rows get `last_used_at`. Rows without a vector (written while Mesh was down) are matched lexically even in vector mode. Prompts render memories in their DYNAMIC section inside `<memory>` tags (untrusted).

### Writing memories (`writer.py`, `prompts/memory_writer/extract.v1.md`)
- **Chat:** the Celery tick `memory_consolidation` (every 15 min) picks sessions idle ≥ 15 min with turns after `consolidated_at`. The fast LLM extracts at most 6 candidates (kind, topic, text, importance, confidence, expires_on). Code drops low confidence (< 0.6), blocked content and already-past expiries, then writes them through the rules above. The watermark is the newest message actually read. Without Mesh the session stays pending (retried next tick).
- **Prompt rules:** only things the USER said about themselves; never FinPilot's figures, small talk or credentials; relative dates become absolute; "how to advise / what to avoid" is a preference.
- **Recommendation accept/dismiss:** a deterministic `decision` memory (`templates/messages/memory.yml`) in slot `advice_<candidate key>`. It is written by a Celery task, so the click never waits for an embedding call, and a later change of mind supersedes it. The advisor's existing candidate-key dismissal memory (Phase 6) still prevents repeats; the new memory carries the user's reason to the chat agent and the advisor prompt.
- **Advisor:** `advisor/recommend.v2` adds the user's relevant memories (query = the candidates' kinds and entities) and a rule to respect stated preferences ("Netflix is shared with family"). The template fallback ignores memories; dismissals are still enforced by key.

### Short-term memory (`short_term.py`)
- **Window:** Redis `chat:{user}:{session}` keeps the last 12 turns (TTL 24 h). On a Redis miss it is rebuilt from `chat_messages`, so Redis stays a cache.
- **Rolling summary:** turns beyond 12, or beyond a 1,500-token budget, are folded into `chat_sessions.summary` by the fast LLM (`prompts/chat/summarize.v1.md`). Without Mesh it keeps a truncated transcript. The summary drops figures (they can be looked up again).
- **Pending what-if clarifications** live in the simulator's LangGraph thread. The pointer is durable: the last assistant message has `route = simulator` and `status = needs_input` in `tools_json`. A reply that matches another canonical intent abandons the pending question instead of being taken as its answer.

### Chat agent (`finpilot/agents/chat_agent.py`, `chat_tools.py`, `config/chat_tools.yml`)
- **Paths, cheapest first:**
  1. A pending what-if → resume the simulator.
  2. "Can I afford …" → the simulator (never cached).
  3. A canonical question recognised by rules or embeddings → the Phase 8 answer service (L2/L3).
  4. Otherwise a Mesh tool loop (smart model, ≤ 6 model turns).
- **Canonical vs specific:** a canonical answer covers the whole picture. If the question names one of the user's categories or top-100 merchants (word overlap after removing generic words listed in `config/intents.yml`), it goes to the tool loop instead. The first e2e run showed why: "How much did I spend on dining in the last 3 months?" got the overall spending-trend answer. The dashboard ask box adds an "ask in Chat" link for such questions.
- **Tools:** `query_transactions` (period / filters / group_by / compare_previous / largest), `get_category_trend`, `get_forecast`, `simulate`, `list_subscriptions`, `get_goal_status`, `list_recurring`, `explain_recommendation`, `recall_memory`, `convert_currency`.
  - `convert_currency` is backed by the FX service, which uses the FX MCP server, so MCP output reaches the model only as tool data; the LLM call itself still goes through Mesh.
  - Tool descriptions and JSON schemas live in config (model-facing text). Implementations are deterministic and run in a savepoint; errors come back as `{"error": …}` for the model to recover from.
  - Tools return derived figures (previous-period change and %, period length in days and months) so the model never has to compute.
- **Hygiene:** every tool result goes back to the model as `<data>…</data>` and into a FactSet. The final text must pass `trail/grounding.check`; failures are retried twice, naming the unverified numbers.
  - Then a deterministic fallback: headlines of the tool results (`templates/messages/chat.yml`).
  - Numbers in the user's own message count as facts; memory texts are masked as literals.
  - Unverified text is never streamed: SSE (`POST /chat/stream`) sends `session`, `progress` (one per tool, e.g. "Looking at your Dining spending…"), then the verified `message`.
- **Not cached:** tool-loop answers (they depend on conversation context and often on user-given amounts).
- **Degraded mode (no Mesh):** canonical questions still work (templates), what-ifs say the simulator is unavailable, and anything else gets a clear notice.
- **Coordinator:** `route_intent` gained `answer_from_cache` and `free_chat` (thin nodes over the same services), reachable through `POST /pipeline/run`. The interactive chat API calls the agent directly so it can stream progress.
- **Feedback:** 👍/👎 per assistant message → `chat_messages.feedback` + Langfuse `user_feedback` score. The trace carries `session_id` = the chat session.

### Eval suite 8 (`eval/suites/memory.py`, `eval/datasets/memory_facts.yml`)
- **Data:** 16 facts, 20 user-style questions (many share no words with the fact), 3 contradictions, 5 sensitive strings, 4 synthetic chats for the writer and 10 chat questions on the persona sample (`eval/synth/persona_sample.py`). Written once, before the first run.
- **Offline (lexical):** hit@1 0.50, hit@5 0.70. Every miss was a pure paraphrase ("safety net" → emergency fund). Supersede, deletion, blocked content and isolation 100%.
- **Full (Mesh embeddings, first run):** hit@1 0.90, hit@5 0.95 (miss: "Should I budget for fuel and car servicing?" → "no longer owns a car"). Writer recall, precision and no-leak 100% (5 of 5 expected, nothing extra, no quoted figures or account numbers). Chat: 10/10 answers with every number verified, no fallbacks, p50 3.5 s, p95 7.5 s.
- **Full, committed run (`2087664`):** retrieval and writer as above (hit@1 0.90, hit@5 0.95; writer 100%). Chat 9/10 answers with every number verified; 1 fallback ("How does my dining spend compare with the 3 months before?": the draft used a figure no tool returned, twice, so the user got the verified headline instead). p50 3.1 s, p95 8.8 s. After the rule fix, "Which category grew the most last month?" correctly names Dining Out (+689%). LLM sampling varies between runs (the first run had 10/10).
- **Disclosed fix on eval data:** the same run answered "Which category grew the most last month?" with the top-categories template (grounded but off-topic) because the rule `which categor` matched first. A `grew|increased|rising|went up` pattern was added to `spending_trend` (earlier in file order). "Grounded" measures that numbers are real, not that the answer is relevant; relevance was checked by reading the 10 answers.

## 12. Phase 10: scheduler ticks, digests, risk alerts, delivery adapters

### Ticks, not per-user cron
Beat runs a fixed set of entries (`finpilot/tasks/celery_app.py`). Each tick selects the users it applies to, and each takes a `lock:beat:*` Redis lock so it never runs twice concurrently. Synthetic (eval) users are always excluded.

| Entry | When | What |
|---|---|---|
| `digests-due` | every 15 min | users whose weekly slot has come and whose digest for that period is not `sent` → a pipeline run (trigger `schedule`) |
| `risk-alerts` | hourly (:05) | users with alerts on and any transactions → low-balance check |
| `refresh-insights` | daily 01:00 UTC | active users (login or new transactions in 30 days) → pipeline run (+ answer pre-warm), spread 2 s apart |
| `goal-snapshots` | Mondays 01:30 UTC | a weekly point per goal even without data changes |
| `cache-gc` | nightly 03:45 UTC | semantic-cache rows of old data versions or > 30 days, answer_log > 90 days, job_runs > 30 days, used/expired Telegram tokens, outbox files > 14 days |
| `memory-consolidation`, `materialize-recurring`, `refresh-fx-rates`, `nightly-eval-offline`, `heartbeat` | as before | |
| `telegram-poll` | every minute, only with `TELEGRAM_POLLING=true` | binds chats without a public webhook |

### Digest timing (`finpilot/digests/schedule.py`, pure functions)
- **Slot:** the user's weekday (0 = Sunday) and hour, in THEIR timezone (`zoneinfo`; an invalid zone falls back to UTC).
- **Period:** `period_key` = the local date of the most recent slot. A digest is due from the slot for 24 h, so it catches up after downtime but never sends a stale one days later.
- **DST:** "09:00 local" stays 09:00 across DST changes because slots are built in local time.
- **Exactly once:** `UNIQUE (user_id, period_key)`, plus a `lock:digest:{user}:{period}` lock against concurrent senders. A `failed` row is retried by the next tick; a `sent` row is never replaced.

### Digests come from a pipeline run
- `digests-due` does not compose the digest itself. It enqueues a scheduled pipeline run, and the `deliver` node (the last step) sends the digest from the data that run just refreshed.
- A degraded run produces a digest that says so.
- `deliver` runs only for scheduled runs, and only when the slot is due.

### Digest content (`finpilot/digests/service.py`, `templates/messages/digest.yml`, `digest.txt.j2`)
- **Sections:**
  - ONE key insight: the dashboard's top finding.
  - ONE action: the top proposed recommendation, with its simulated effect on the expected balance from the decision trail's forecast reference.
  - Goals: status and percentage, plus "± since last week" from `goal_snapshots`.
  - The next 14 days of bills and incomes.
  - Notices: degraded run; exchange rates not live.
- **Phrasing:** only the insight and action sentences may be LLM-phrased (`prompts/digest/phrase.v1.md`, fast model), through `guarded_json` with template fallback.
- **Found live (fixed):** the first Mesh run fell back three times. Its facts were the finding and action *sentences*, and numbers inside strings were not registered as facts. Those sentences are deterministic renderings of computed values, so `facts_from_text` now registers their figures. The next live run passed grounding on the first attempt.
- **Preview:** `POST /digest/preview` (admin) stores a preview under its own key (`pv-…`, status `preview`). It is only shown in the app, labelled "[Preview]", and never delivered.

### Channels (`finpilot/delivery/`)
- **in_app:** always stored; shown in the Inbox.
- **email:** SMTP when configured, else the log sink (`outbox/*.json`).
- **telegram:** Bot API `sendMessage` (plain httpx adapter, no MCP per AMENDMENT_1) when a bot token is set, else the log sink.
- **Telegram not linked:** the digest stays in-app with a note asking the user to link it.
- **Failure:** a channel error marks the digest `failed` (content kept) and the next tick retries.

### Telegram linking
- **Link:** `POST /settings/telegram/link` creates a single-use token. Only its HMAC is stored, and it expires after 30 minutes. The response is the deep link `t.me/<bot>?start=<token>`.
- **Binding:** the bot receives "/start <token>" through the webhook `POST /telegram/webhook/{secret}` (path secret derived from SECRET_KEY, compared in constant time, hidden from OpenAPI, needs no cookies or CSRF) or through the polling task. `handle_update` binds the chat through the SECURITY DEFINER function `telegram_bind`, the only pre-auth write.
- **Unlink:** `DELETE /settings/telegram` also moves a Telegram digest channel back to in-app.

### Risk alerts (`finpilot/digests/alerts.py`)
- **Trigger:** the forecast's bad case (p10) falls below the user's cash buffer within 14 days. Severity is critical if it goes below zero, else warning. The alert names the first day below the cushion, the low point and the bills before it.
- **Dedupe:** `dedupe_key = low_balance`, at most one alert per 7 days.
- **Delivery:** on the digest channel; always in the Inbox, plus a dashboard banner while unread.
- **Bug found by the alert test (fixed):** the forecast snapshot was reused after the user changed their cash buffer. The buffer is a setting, not data, so no data_version bump happens. `get_forecast` now treats a snapshot with a different buffer as stale, which also fixes the forecast verdicts.

### Scheduler evidence
- **`job_runs`:** written for every `finpilot.*` task through Celery signals. The signal module is imported by the Celery app itself, so eager (API/test) processes record runs too, not only workers.
- **`GET /scheduler/jobs` (admin):** each beat entry with its schedule, last run and status, an estimated next run, and recent results. The same table is shown on the Pipeline page for admins.

### UI
- **Header:** an Inbox bell with an unread count (unread digests + alerts). The user's name moved to ≥ 2xl widths, because at 1280 px the bell pushed "Sign out" off-screen (seen in the e2e screenshot).
- **Inbox page:** schedule summary, alerts with "mark read", digest history and the digest reader.
- **Settings:** a Telegram link/unlink card.

## 13. Phase 11: app shell, theme-aware colours, accessibility, Evaluation screen, FX & MCP suite, your data

### App shell (user request)
- **Layout:** a left sidebar replaces the 12-link top bar, with the same bluish palette.
  - Grouped navigation: Overview · Money ▸ (Transactions, Recurring, Categories) · Planning ▸ (Forecast & goals, Advice, Insights) · Assistant ▸ (Chat, Memory).
  - A "Tools" section: Import, Pipeline, Quality, Settings.
  - A "+ Add" menu: expense, income, import.
- **Groups:** native `<details>` elements (keyboard and screen-reader friendly with no JS), open when they contain the current page. The structure is `views.NAV`; the partial is `partials/sidebar.html`.
- **Top bar:** reporting currency, light/dark toggle (sun/moon), Inbox bell with an unread count, and an account menu (Settings, Inbox, Memory, Sign out).
- **Mobile (< 1024 px):** the sidebar becomes a drawer (Alpine, closes on Escape or a backdrop click).
- **Dashboard:** a "Quick add" row (expense, income, import, ask) replaces the two header buttons, as in the user's reference design.
- **Icons:** one inline-SVG macro (`partials/icons.html`), always `aria-hidden`.

### Colours that work in both themes
- **Baseline:** `make a11y` (axe-core 4.10.2, WCAG 2.1 A/AA, every page in light AND dark mode) found 838 violations, 640 of them dark-mode text contrast. Causes: slate-400/500 secondary text on dark surfaces and on tinted light ones, brand-600 links on dark, emerald-600 amounts on white, rose-600 in dark.
- **Fix: semantic text tokens** (`/static/css/theme.css`, mapped in `tw-config.js`): `text-muted`, `text-soft`, `text-accent`, `text-pos`, `text-neg`, `text-warn`. Each is a CSS variable with a light and a dark value, each checked at ≥ 4.5:1 on every surface it is used on.
  - A one-off script replaced the per-template shades in templates and JS strings. It left tinted "box" patterns alone, because those carry their own `dark:` text.
  - Tinted badges got dark backgrounds, scroll regions became focusable (`tabindex=0`), and links inside text are underlined.
  - Rule from now on: never use raw slate/brand/emerald/rose shades for TEXT.
- **Result:** 0 violations on 17 pages × 2 themes.
- **Not audited:** content that only appears after interaction (open modals, chat replies, live pipeline badges). The pipeline badges were fixed by hand.
- **Font:** Inter was declared but never loaded. It is now loaded from pinned `@fontsource-variable/inter@5.1.1` on jsDelivr (CSP `style-src`/`font-src` allow it), with tabular numbers in table cells.
- **Charts:** one `fpChartTheme()` sets Chart.js text and grid colours and re-colours existing charts when the theme is toggled. Before, charts kept the colours they were created with.

### Evaluation screen
- **Tabs (ARIA tablist, arrow keys, `#hash` deep links):**
  - Overview: trend of key metrics across the last 15 runs of the same mode, quality gates, insights, and suites with failing-case counts.
  - One tab per suite.
  - Failures: any suite's cases, failing or all, searchable, with a trace link for admins.
  - Compare runs: per-metric baseline vs candidate with the change; green/red respects lower-is-better metrics.
- **API:** case search covers case ids and inputs; `/eval/compare` marks lower-is-better metrics.

### Eval suite 9: FX & MCP (`eval/suites/fx.py`, `eval/datasets/fx_reference.yml`)
- **Reference table:** fixed INR-per-USD rates in January 2001. No real stored rate is that old, so the suite can run against the dev database. It deletes its rows and fetch-log entries afterwards, leaving the app's FX banner as it was.
- **Checks:**
  - 8 conversions with hand-computed expected values (round half even);
  - USD → INR → USD within $0.01 (INR → USD → INR can only be exact to one cent × the rate);
  - Saturday, Sunday, New-Year holiday, business-day and future-date resolution;
  - four fallback scenarios (source and stale/approximate flags);
  - MCP contract against the in-process FastMCP server (tools = allowlist, response shapes, unsupported currency is a tool error that does not trip the breaker, caching);
  - allowlist before I/O; circuit breaker open → half-open → closed;
  - grounding of converted amounts, using exactly what the chat agent's `convert_currency` tool returns;
  - `fx_exposure` on the two cross-currency personas vs ground truth computed from the generator's own transactions and FX series.
- **Found while building it:**
  - **Real bug (fixed, regression test):** `FXService.rate_to_inr` accepted a stored rate up to 4 days before the requested date, even when the days in between were business days nobody had fetched yet. A Friday entry could get Tuesday's rate after an earlier lookup stored rates only up to Tuesday. `backfill`'s coverage check had the same right-edge flaw.
    - Now an earlier stored rate counts only if the gap is weekend-only, or if a later stored rate within one fetch window brackets the date (the gap is a known holiday).
  - **Rates were not groundable:** the chat tool reported the rate as a bare float, so "46.75 rupees per dollar" could not be grounded. It is now an amount in the target currency.
  - **Suite harness, not product:** the fx_exposure check must use the loader's history split and true categories. Uncategorized savings transfers would otherwise count as spend; that is a categorizer effect, not an FX one.
- **Result:** all ten metrics at 100%, gated at 1.0 (deterministic).

### Your data (deferred from Phase 1)
- **`GET /me/export`:** one JSON file with the profile and every user-owned table, read in an RLS-scoped session. Password/token hashes, vectors and the tsvector are excluded.
- **`DELETE /me`** and the Settings "Delete my account" form (password + typing DELETE):
  - one `DELETE FROM users`, which every user table cascades from;
  - access tokens revoked and the user's Redis keys removed;
  - an anonymous `account_deleted` audit event with a hashed id, since the user's own events cascade away.


## 14. Phase 12: release hardening

- **Held-out personas for the analyzer:** the same five persona definitions with new seeds (offset 7919), never inspected, are scored with the same matching logic. Results:

  | | Tuned set | Held-out |
  |---|---|---|
  | Precision | 0.56 | 0.53 |
  | Recall (material) | 0.86 | 0.78 |
  | False alarms per clean month | 0.20 | 0.28 |

  The held-out set is held to the bounds committed in Phase 4; no new bounds were picked after seeing these numbers.
- **Production (`docker-compose.prod.yml`, `deploy/caddy/Caddyfile`, DEPLOY.md):**
  - Caddy is the only published service (80/443, HTTP/3). Postgres, Redis and the Langfuse ports are reset with `!reset`.
  - `APP_ENV=prod` makes the app refuse to start without an https base URL and a 32+ character key.
  - `make prod-check` refuses every vendored dev default (database passwords, Langfuse SALT, encryption key, NextAuth secret, MinIO and Redis passwords, init user, API keys); `make prod-secrets` prints fresh ones.
  - Verified locally with Caddy's internal CA: HTTP/2, HSTS, Secure cookies, HTTP → HTTPS redirect.
- **Found while testing prod (fixed):**
  - Uvicorn trusted `X-Forwarded-For` only from 127.0.0.1. Behind the Caddy container every user would have shared one rate-limit and lockout bucket (Caddy's IP). The prod override sets `FORWARDED_ALLOW_IPS=*`, safe because only Caddy can reach the app, and the app log now shows the real client IP.
  - `.dockerignore` did not exclude `backups/`, so database dumps would have been baked into images built after a backup. Now excluded, and verified on a built image.
  - Caddy rejects an empty `email` directive (an empty env value counts as set); the default moved into the compose file.
- **Backups:** `make db-backup [DB=langfuse]`; `make db-verify-backup` (restore into a scratch DB, compare row counts, drop); `make db-restore` (stops app/worker/beat, single transaction). Verified both: a dev backup restored with identical row counts in 9 main tables, and a full restore into the trial prod stack served the data after the app restarted.
- **Definition of done, checked on the running stack:**
  - With fx-mcp stopped, a USD expense by an INR user converted through the direct-HTTP fallback and shows both amounts in INR and USD views.
  - With both live routes down, it converted at a stored rate and the UI showed "Live exchange rates are unavailable right now".
  - Two FX status bugs found there (fixed, with tests):
    - The footer named the source of the newest stored row ("live (FX MCP)") instead of the route that worked last.
    - With both live routes failing, no banner appeared while the stored rates were young.
  - A 15-minute `fx-recheck` tick now probes the live routes only while rates are not live, so the banner clears soon after recovery. Before, it could last until the next twice-daily refresh.
- **Cleanup:**
  - CI and `make lint` type-check `finpilot mcp_servers eval`, but during Phases 8–11 mypy had been run on `finpilot` only. The eval suites had 18 errors (fixed); CLAUDE.md now lists the full command.
  - vulture (dead code, 80%+ confidence) clean; gitleaks on 25 commits of history found no leaks (the only working-tree hit is the gitignored `.env`).
  - Every Settings field is documented in `.env.example`.

## 15. After Phase 12: home screen, demo accounts and sample data (user request)

### Home screen
- **Removed:** the "Ask FinPilot" box (the *Ask FinPilot* quick-add tile opens Chat, which has the same instant cached answers) and "Key insights" (the Insights page has them; the weekly digest still uses the top finding).
- **Low-balance alert:** one line with an icon, the body truncated (full text on hover), *View forecast* and a × that marks it read (`POST /alerts/{id}/read`).
- **Period:** a dropdown (`<details>`, works without JS) instead of five side-by-side links.
- **Trend chart:** starts at the first month with activity. A 0% change reads "same as previous", not a red ▼.

### Admin-only screens
Pipeline and Quality are operator screens. `views.NAV["tools"]` entries carry an admin flag; the sidebar hides them from other users and `_admin_required` answers a 404 page. Their JSON APIs are unchanged: `/pipeline/*` is the user's own runs, and `POST /eval/run` and `/scheduler/jobs` were already admin-only.

### Sample data (`data/sample/`)
- **Why files, not the generator:** the user asked for sample data that lives in the repo, so evaluators can see it and load it, with plain descriptions (no "UPI/…/…" bank strings). Three datasets: INR only, USD only, and INR + USD with INR reporting (a US freelance client paid in dollars and dollar-billed software, which exercises FX conversion and the foreign-currency exposure finding).
- **Format:** the import template's columns, every row categorized (`cat_source='import'`), so a demo works without Mesh. Built by `scripts/build_sample_data.py` (seeded, deterministic); `datasets.yml` lists labels and the recurring items each declares.
- **Dates:** 2026-04-01 → 2026-10-15. Loading moves them by whole months so the last file month is the current month (rows after today are dropped), as before.
- **Planted for Insights:** a same-day duplicate, a large first-time purchase, a price rise on a subscription (INR, USD), a bill spike and a heavy food-delivery month. `tests/test_sample_data.py` loads each dataset and requires the duplicate, first-time-large and price-hike (INR, USD) or FX-exposure (mixed) findings, 6+ months with spending, and a clean undo.
- **Tuned once by eye:** the first INR draft spent almost all income, so the forecast showed a 100% chance of dropping below the cushion. Salary went from ₹1.25 L to ₹1.5 L and two dining streams were thinned; the mixed dataset's freelance income was halved. Monthly savings now range about 18–25% (−20% in the laptop month).
- **Undo also removes the recurring items** the dataset declared (their ids are stored on the batch) and anything they materialized. Before, undoing sample data left salary and rent rules behind, so the forecast kept them.
- **Demo accounts:** `make seed-demo` creates `admin@` (admin, no data), `inr@`, `usd@` and `mixed@finpilot.local` with one shared dev password. It refuses to run in production with the default password. `make e2e` and `make a11y` run it first and use `admin@` for the admin-only pages.
- **The CSV template's examples** now use plain descriptions too. The normalizer still handles bank strings; its tests keep them.


### Forecast: one-off investments and "Projected savings" (user report)
- **Symptom:** a user who moved ₹30 L into an FD, an FCNR deposit and lump-sum funds saw "Projected savings −₹3.2 L/month" next to a 67% savings rate on the dashboard.
- **Causes:**
  1. The bootstrap resampled every unscheduled row, so those one-off transfers became a −₹7.7 L/month "Savings/Investments" stream and a fat bad tail (lowest point ₹63 L against a typical ₹1.0 Cr).
  2. "Projected savings" was the change in *cash*, so money moved into investments counted against it. The dashboard counts the same money as saved.
- **Fix (engine version 2; older snapshots are recomputed):**
  - Unscheduled rows in `savings` categories are no longer resampled; their recent monthly average is reported as `unprojected_savings_monthly`, and the card says so.
  - Scheduled ones (SIP and EPF rules, detected series) stay in the cash forecast.
  - `projected_monthly_savings` = change in cash + scheduled moves into savings, i.e. income minus spending, the dashboard's definition. Goals use it.
  - For that user: +₹6.06 L/month (incl. ₹1.52 L SIP); lowest bad-case point ₹1.00 Cr.
- **Evals:** offline gates unchanged, 46/46; forecast coverage 0.770. The eval personas schedule their investments, so they had no unscheduled ones to drop.

### Chat scope guardrail (user report: the assistant told a joke)
Before this, every message the rules did not recognise reached the tool loop, and the only limit was one prompt line ("Stay on the user's finances").

**Three layers, cheapest first.** Each ends in the same fixed reply (`templates/messages/chat.yml` `out_of_scope`), with no model-written text.
1. **Rules:** intents `off_topic` and `smalltalk` in `config/intents.yml`.
   - They are listed after every finance intent, so a finance pattern always wins.
   - The patterns are deliberately narrow. Sports names and the bare word "song" were dropped after review because they would refuse "How much did I spend on cricket gear?" and "my song subscription".
   - No model call at all.
2. **Router LLM:** the same intents with examples. `intent_router/classify.v2` describes what FinPilot covers. General personal-finance questions ("what is an SIP?") stay `free_form`.
3. **Tool loop:** a `decline_off_topic` tool and a scope rule in `chat/agent.v2`. When the model calls the tool, the agent stops and returns the fixed reply.

**Smalltalk** (greetings, thanks, "what can you do?") gets a short fixed capabilities reply.

**Answers API:** `POST /answers` uses the same intents. Chips and the pipeline's `answer_intent` accept only `intent_router.answerable()`.

**Eval** ("Memory & chat" suite; dataset written before the first run, gates set before it):
- 12 off-topic requests: 6 are caught by rules, the rest are paraphrases.
- 16 in-scope questions: the 10 chat questions plus 6 tricky ones (cricket gear, capital gains, movie tickets, SIP).

| Metric | First full run | Gate |
|---|---|---|
| Off-topic refused | 1.0 (12/12) | ≥ 0.90 |
| Money questions wrongly refused | 0.0 (0/16) | ≤ 0.05 |
| Rules alone refusing a money question (offline too) | 0.0 | 0 |

Router accuracy (0.971) and chat grounding (0.90) were unchanged.

### Guardrails: PII masking, leak check, advice scope (user request)
**Already in place, and the ones that matter most for FinPilot:**
- The grounding guard.
- Schema-validated outputs.
- Structural injection defences: untrusted text inside `<data>`, allow-listed tools and MCP calls, read-only chat tools.
- The "names a merchant/category/goal" validator.

A transaction description such as "IGNORE PREVIOUS INSTRUCTIONS" is contained by that structure. A classifier that tries to spot it is not relied on.

**Added:**
1. **PII masking on the way out:** Presidio 2.2.364 (`finpilot/privacy/pii.py`, `config/privacy.yml`).
   - **Where it runs:** in `LLMClient._mask` on every prompt (chat, categorizer, advisor, router, narration, digest) and on embedding inputs, before the L1 cache key, so cached and sent prompts are both masked. Langfuse payloads are masked too (Presidio, then the older regexes as a safety net).
   - **What it masks:** card numbers (Luhn), IBAN, Aadhaar (Verhoeff), PAN, US SSN, email, phone (libphonenumber, IN/US), plus custom recognizers for account numbers after an account label, UPI IDs and IFSC codes.
   - **Names are deliberately not masked.** Transaction descriptions need merchant and payee names for categorization, and named-entity models mislabel merchants as people or organisations.
   - **What it leaves alone:** amounts, dates, transaction references and UUIDs, because the grounding guard and the categorizer need them.
   - **No model download:** the analyzer runs on `spacy.blank("en")`, a tokenizer only. Every recognizer used is pattern- or checksum-based, so the 500 MB spaCy model is not needed.
   - **Cost:** about 1 ms per message; the image grows by about 150 MB.
   - **Switch:** `PII_MASKING` (default on).
   - **Dependency override:** `presidio-analyzer` caps numpy below 2.5. `[tool.uv] override-dependencies` keeps numpy 2.5.4; the tests and the full eval verify they work together.
   - **Effect on categorization:** none in the full run (accuracy 0.874, macro-F1 0.841).
2. **Output leak check:** `check()` in `trail/grounding.py` also reports identifiers found in the text.
   - A report with identifiers does not pass, so `guarded_json` and the chat tool loop retry ("remove the credit card"), then fall back to the template.
   - It covers chat, advice, the Insights summary, what-if narration and digests.
3. **Langfuse scores:** `pii_masked` (count, with the entity types as the comment), `pii_leak_blocked`, `advice_scope_blocked`, next to `grounding_pass`.
4. **Advice scope:** `config/guardrails.yml` patterns for investment, tax and legal directives, plus prompt rules (`chat/agent.v3`).
   - The check runs on every chat answer and in the advisor's validator: retry with the message, then fall back.
   - FinPilot's own levers still pass: cut a category, cancel a subscription, delay a purchase, move money to savings.
5. **Eval suite `guardrails`** (deterministic, both modes, dataset `eval/datasets/guardrails.yml`, written with the implementation, so the gates are regression gates):
   - 17 identifiers masked, 14 figure-only strings untouched, 5 leaking answers blocked, 5 clean answers passed, 10 directives flagged, 11 levers passed, injected text inside `<data>` in the categorizer and router prompts.
   - Masking p95 under 1 ms.
   - All at 100%.

**Found while building:**
- **UPI ID at the end of a sentence:** "…@okhdfcbank." was missed (the full stop looked like an email domain). The pattern was fixed and the case added.
- **Pre-existing flaky test:** chat turns stored in one transaction share `now()`. Ordering by `created_at` alone left a user turn and its answer in random order. Ties now put the user turn first, in the short-term window and in the session view.

**Tuned on eval data (disclosed):**
- The first full run failed one pre-set gate: 1 of 16 in-scope chat questions was declined ("What are my capital gains this year?"). The tool-loop model read the new advice-scope rule as "decline".
- `chat/agent.v3` and the `decline_off_topic` description now say questions about the user's own investments or taxes are never off-topic: answer what FinPilot can, say what it does not track, and point to an adviser.
- A probe found the same for "Should I put my savings into bitcoin?". It was added to the in-scope set after the fact.
- Second full run: refused 12/12 off-topic, wrongly refused 0/17, and all 62 gates passed.
- The advisor judge moved from 0.967 to 0.933: one persona fell back to templates after the existing "name the category specifically" check. Two reproductions with the same code did not fall back, and the new checks never fired, so this is run-to-run variance.

**Found by this eval, fixed in the next change:** "How much did I spend on cricket gear?" was answered from the answer cache with total spending labelled as "cricket gear". See "Specific questions vs canonical answers" below.

### Specific questions vs canonical answers
- **Cause:** "How much did I spend on cricket gear?" matched `spending_trend` by its rule ("how much did I spend").
  - `names_entity` only sent a question to the tool agent when it named one of the user's existing categories or merchants. "Cricket gear" is neither, so the canonical answer (overall spending) was served.
  - Its LLM phrasing, which sees the question, wrote "₹73,185 on cricket gear". The number was real, so the grounding guard could not catch the wrong label.
- **Fix, two layers:**
  1. **Routing:** a question is also specific when it has a content word that no canonical question uses. The vocabulary is the answerable intents' examples and pattern words, plus `question_words` and `generic_entity_words` in `config/intents.yml`. Such questions go to the tool agent ("no category named Cricket Gear…").
  2. **Answer guard (deterministic):** a canonical answer may not contain a specific word from the question that its facts do not contain (`answers/service.py::_borrowed_words`). The answer is retried with the reason (`templates/messages/answers.yml` `borrowed_words`), then the template is used. This also covers the answers API, where routing does not apply.
- **Trade-off:** rephrased canonical questions with unusual words ("how's my spending looking lately?") now go to the tool agent. That costs more but is still correct.
- **Full run:** cache hit rate (0.875) and router accuracy (0.971) unchanged; chat grounded 0.90; 62/62 gates.
- `advisor.fallback_rate` is now lower-is-better in the run comparison (its drop to 0 was reported as a "regression").
- **`query_transactions` with a category that does not exist** ("Movie tickets") now searches descriptions for the same words instead of returning an error. The result carries `no_such_category_searched_descriptions` so the model can say so. Before, the agent answered "no category named Movie Tickets" although the user had such transactions.
- **The advisor's LLM-judge score is noisy.** Over four full runs with unchanged advisor code and prompts it read 0.967, 0.933, 0.956 and 0.878; the last run had no fallbacks and a 100% first-attempt grounding pass. The gate (≥ 0.70) is far below that spread; read single-run moves of a few points as noise.

## 16. Date pickers and transaction period filters (user request)
- **Date fields:** every date input carries `x-datepicker` (`static/js/datepicker.js`), which wraps it with **flatpickr 4.6.13** (jsdelivr, already allowed by the CSP; loaded for signed-in pages only). The visible field shows "15 Oct 2026" and accepts typing; the hidden original keeps ISO `YYYY-MM-DD` for forms and `x-model`. Typed input is parsed leniently: ISO, day-first numbers (`15/10/2026`, `15-10-26`), `15 Oct [2026]`, `Oct 15[, 2026]`; impossible dates (31/02) are rejected. Weeks start on Monday. On phones flatpickr falls back to the native picker; if the CDN is blocked the native `type="date"` input remains.
- The directive mirrors model changes made by code (prefills, resets) into the picker, comparing against the picker's selection (x-model has already written the hidden input). Colours: `theme.css` overrides, light and dark.
- **Transactions period:** `?period=` ∈ `transactions.service.PERIODS` (any time, this/last month, last 7/30/90 days, this/last year, custom), resolved in the user's local timezone by `period_range`. Calendar periods cover the whole month/year. A preset wins over the From/To dates the form also sends; "Any time" ignores them; links with dates but no `period` (insight evidence, old bookmarks) count as custom. Choosing a preset submits at once; "Custom range" shows From/To, prefilled with the last range.
