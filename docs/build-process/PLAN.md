# FinPilot — Build Plan

Sources: `BUILD_PROMPT.md` (base spec) + `AMENDMENT_1.md` (overrides). **Where they conflict, the amendment wins.**
This file is the merged, authoritative plan. Status markers: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` cut.

Priority legend: **P0** required · **P1** differentiator · **P2** stretch (cut first).

---

## 1. Resolved conflicts (amendment wins)

| Topic | BUILD_PROMPT said | Now (AMENDMENT_1) |
|---|---|---|
| Database design | "SQLAlchemy + Alembic" data model | **PostgreSQL-first**: Alembic migrations hold real DDL; SQLAlchemy 2.x (psycopg3) is only the access layer |
| Primary keys | unspecified | `uuid DEFAULT gen_random_uuid()` for user-facing entities; `bigint GENERATED ALWAYS AS IDENTITY` for `llm_calls`, `agent_runs`, `eval_metrics` |
| Money type | `NUMERIC(14,2)` | `NUMERIC(18,2)`; FX rates `NUMERIC(18,8)` |
| Column types | — | `citext` email, `JSONB` (GIN only where queried), CHECK/ENUM for status/kind, partial unique indexes |
| Row-Level Security | P2 | **P1**: RLS on every user-owned table, `SET LOCAL app.user_id`, app connects as non-owner role; admin/system role with audited bypass |
| Analytics queries | unspecified | Plain SQL in `finpilot/db/sql/*.sql` (CTEs, window fns), loaded by a helper; pandas/numpy only for statistics |
| Currency model | One currency per user, **no FX**, mismatched import row = error | **Base currency INR**; users have `reporting_currency` (INR default \| USD); transactions/accounts in ANY supported currency; deterministic FX conversion |
| `users.currency` | `currency` | `reporting_currency` |
| Changing currency | "relabel only" confirmation | Pure VIEW change, no data relabeled; invalidates answer caches |
| Template `currency` column | must equal profile currency | optional, any supported currency (default account → reporting currency); new optional `fx_rate` column; XLSX currency dropdown |
| Reporting queries | read `transactions` | read view `v_transactions_reporting` (`amount_reporting`) |
| Cache key (L2) | `…:{currency}:…` | adds `reporting_currency` and `fx_asof` (when user has foreign-currency data) |
| Grounding facts | amounts | also original amount, converted amount, rate, rate date |
| README limitations | "no FX" | "rates are daily reference rates, not your bank's card rate; use per-txn override" |
| MCP | none | Runtime FX MCP server (P0) + MCP client/registry; dev-time `.mcp.json` |
| Makefile | — | + `db-backup`, `db-restore` |
| Eval suites | 8 | + suite 9 **FX & MCP** |

Unchanged: Mesh-only rule, numbers-from-code, prompts outside Python, prompt-injection hygiene, strict tenancy, uv tooling, compose-everything, Langfuse, LangGraph on Redis, caching layers, memory, scheduler, eval rigor.

---

## 2. Architecture snapshot

```
browser ──htmx──► app (FastAPI+Jinja) ──► Postgres 16 + pgvector (RLS)  ◄── worker/beat (Celery)
                     │    │                                                  │
                     │    └── Redis (0 app/cache/LangGraph ckpt, 1 broker, 2 results)
                     │
                     ├── finpilot/llm/client.py ──► Mesh API (ONLY LLM path) ── traced by Langfuse
                     └── finpilot/mcp/client.py ──► fx-mcp (FastMCP, streamable HTTP) ──► FX reference API
                                         fallback: direct HTTP → fx_rates (stale) → config/fx_fallback.yml (approx)
Langfuse v4 stack (web, worker, clickhouse, minio, redis, postgres/langfuse db) via compose `include:` (see DESIGN_NOTES §0)
```

LangGraph pipeline: `ingest → categorize → analyze → forecast → goals → advise → persist_trail → snapshot → (deliver?)`, entry `route_intent`.

---

## 3. Phases

Rule: tests green before moving on; commit at end of each phase.

### Phase 0 — Foundations (P0)  `[x]` 
- [x] `pyproject.toml` (Python 3.12, uv, hatchling), pinned via `uv.lock`; `requirements.txt` via `uv export --no-hashes` (`make check-reqs`)
- [x] `finpilot/config.py` (pydantic-settings; incl. `BASE_CURRENCY`, `FX_MCP_URL`, `FX_SOURCE_BASE_URL`, `FX_PROVIDER`, `FX_STALE_AFTER_DAYS`, 3 DB role URLs)
- [x] `.env.example` (every var documented), `.gitignore`, `.dockerignore`
- [x] Dockerfile (multi-stage, official uv image, non-root, `uv sync --frozen --no-dev`, migrations on start under advisory lock)
- [x] `docker-compose.yml`: `app`, `worker`, `beat`, `fx-mcp`, `finpilot-postgres` (pgvector pg16 + init: roles `finpilot_app` / `finpilot_admin` (BYPASSRLS) / `finpilot_ro`, db `langfuse`, extensions), `finpilot-redis` (Redis 8); Langfuse v4 via `include:` with headless init
- [x] Alembic baseline 0001 (raw DDL: extensions, default privileges, `llm_calls`, `fx_rates`, `fx_fetch_log`)
- [x] `finpilot/llm/client.py` — sole Mesh client (Langfuse OpenAI wrapper), tenacity retries, timeouts, JSON (+1 repair) / tools / embeddings, `CallRecord` sinks → `llm_calls`, L1 cache hook, embeddings probe → lexical fallback, `FakeBackend`
- [x] `finpilot/llm/prompt_registry.py` + `prompts/system/hello.v1.md`
- [x] `finpilot/observability/langfuse.py` (no-op when unconfigured) + `masking.py`; `scripts/hello_trace.py`, `scripts/prompts_sync.py`
- [x] FX source verified (Frankfurter **v2**, ECB provider pin) → DESIGN_NOTES; `finpilot/fx/provider.py`; `mcp_servers/fx` FastMCP server (4 tools + `/health`); `finpilot/mcp/{client,registry}.py` + `config/mcp_servers.yml`
- [x] `.mcp.json` (Context7, Postgres MCP Pro read-only, Playwright, GitHub, Langfuse docs) — env-var secrets only
- [x] Tests (57): mesh-only, no-inline-prompts, prompt registry, LLM client (fake), observability/masking, FX provider (respx), MCP contract (in-process + real HTTP, allowlist, cache, breaker), infra (roles, SET LOCAL non-leak, llm_calls sink, /health, /ready, Redis JSON)
- [x] Makefile (all targets incl. `db-backup`/`db-restore`, `mesh-probe`, `hello-trace`), CI (lint, mypy, reqs sync, migrate, pytest, gated offline eval hook, optional live smoke)
- [x] Full stack verified: `docker compose up --build` → 11 services healthy/running; app `/ready` ok; live FX through `fx-mcp` (USD→INR 2024-01-07 → 83.15 @ 2024-01-05); Celery worker + beat up; Langfuse headless init + span ingestion confirmed via the API
- [x] Mesh live probe: chat + JSON mode ok, embeddings ok (1536-d) → embeddings enabled; `make hello-trace` → span + generation in Langfuse with hashed user/session. `cached_tokens` inconclusive (prompt < 1,024 tokens) → re-check in Phase 3

### Phase 1 — Auth, tenancy, currency, shell UI (P0; RLS P1)  `[x]`
- [x] Migration 0002: `users` (citext email, `reporting_currency`, role, data_version, lockout fields), `refresh_tokens` (family_id), `password_reset_tokens`, `auth_events`, `user_settings`, `accounts`, `transactions` core (orig amount/currency + `fx_rate`, `amount_inr`, `fx_rate_date`, `fx_source`; CHECKs; partial/unique indexes), `v_transactions_reporting` (security_invoker)
- [x] **RLS** on every user table (`app_current_user_id()` from `SET LOCAL`); pre-auth lookups via 4 SECURITY DEFINER functions (no bypass role in the request path); admin role BYPASSRLS for system jobs
- [x] argon2id + policy (min 10, 10k common list + "common word + digits" stem check); JWT access 15 min (`iat_ms` for revocation); rotating hashed (HMAC) refresh tokens, family reuse detection with 10 s parallel-request grace; Redis revocation hints (`sess:rb:`, `sess:deny:`)
- [x] Cookies (httpOnly, Secure in prod, SameSite=Lax) + double-submit CSRF (always for forms → blocks login-CSRF); Bearer API exempt; transparent cookie-session refresh middleware
- [x] Redis rate limits (login 5/min/IP, register, reset per IP + per email), exponential per-account lockout (Postgres-durable); security headers + CSP; CORS locked to APP_BASE_URL; prod guards (SECRET_KEY ≥ 32, https base URL)
- [x] Forgot/reset (single-use, 30 min, hashed) via email adapter → log sink (`outbox/`); change password revokes other sessions; `auth_events` audit trail
- [x] `config/currencies.yml` + `finpilot/money.py` (Decimal, half-even, Babel `en_IN` lakh grouping), `|money` Jinja filter, `money.js` Intl helper
- [x] FX resolver `finpilot/fx/service.py`: DB → MCP → HTTP → stale DB → pinned, logs every route; nearest-previous-date; future → latest; one-range backfill; status (live/stale/approximate); `/fx/convert|rates|status`; `refresh_fx_rates` Celery task (02:30 & 16:30 UTC + on worker start if stale) — pulled forward from Phase 10
- [x] Web UI shell: base layout (Tailwind CDN, htmx, Alpine, dark mode, no inline JS), login/register/forgot/reset, onboarding (currency + timezone), dashboard empty state + checklist, account settings (profile, password, digest prefs), FX footer + stale/approximate banner, HTML 404
- [x] Tests (138): money, security primitives, FX chain (9), RLS (meta-test: every `user_id` table has RLS; unfiltered queries; cross-tenant writes; view; cascade), auth API (26), IDOR harness (+ meta-test: every non-public route needs auth), web flows (12)
- [x] Live stack verified: migration 0002 auto-applied, form register → onboarding → dashboard, live FX through fx-mcp, **fx-mcp stopped → `fallback_api`**, worker startup refresh → footer "live (FX MCP)"
- [-] Deferred (P1, not blocking): email verification (still deferred); `GET /me/export`, `DELETE /me` done in Phase 11

### Phase 2 — Categories, transactions, manual entry, recurring rules, import (P0)  `[x]`
- [x] Migration 0003: `categories` (system rows `user_id NULL`, shared read-only via RLS; partial unique slug; CHECKs), `hidden_categories`, `merchant_memory`, `recurring_rules`, `import_batches` + `import_files` (raw upload deleted after import), transaction FKs, `dedupe_key`, partial unique `(recurring_rule_id, date)`, trigram index
- [x] **`data_version` by Postgres triggers** (statement-level, transition tables) on transactions/categories/recurring_rules/accounts + reporting-currency change; `user_session` mirrors changes to Redis `dv:{user}` after commit; `current_data_version()` falls back to Postgres
- [x] `config/taxonomy.yml` (26 system categories, INR+USD examples) synced at startup (admin role, upsert by slug)
- [x] Categories: CRUD, hide/unhide system, archive, delete-with-reassign, merge, keyword rules (0.9), instant suggestions (memory → keywords → examples), deterministic recategorize preview/apply (never touches user-set rows)
- [x] Normalizer v1 (`config/normalizer.yml`: UPI/NEFT/IMPS/NACH/POS/SQ*/TST*/CHECKCARD/AMZN…); Phase 3 extends to 60+ tested descriptors
- [x] Transactions: manual entry (positive amount + type), FX resolved on write (`fx_rate`, `amount_inr`, `fx_rate_date`, `fx_source`), edit (re-resolves FX, keeps user override), delete, category correction "apply to this merchant" (merchant memory + past non-user rows), `PATCH /transactions/{id}/fx`, filtered listing via `v_transactions_reporting`, review queue, CSV export with formula-injection guard
- [x] Recurring: pure schedule math (anchor-based, month-end clamp, leap day, weekday alignment, skips, end), CRUD, pause/resume/skip-next/end, edits affect future only, "Repeat this" links the first transaction; materializer (tz-aware, catch-up, idempotent, SKIP LOCKED, stops at a failed date to retry) as Celery beat tick every 15 min under a Redis lock
- [x] Import: template schema/CSV/XLSX (3 sheets + dropdowns for type/category/currency), tolerant parser (encodings, delimiter consistency sniffing, bank preambles, Indian/US/decimal-comma amounts, Dr/Cr, parentheses, debit/credit columns, sign convention, ambiguous dd/mm prompt), column mapping, validation preview (new/duplicate/invalid/warnings, original + INR + rate + date + source), one-range FX backfill per currency, Celery import (all-or-nothing, chunked), undo, history; limits 5 MB / 50k rows
- [x] Dashboard v1 from `finpilot/db/sql/*.sql` (period totals, 12-month trend, category breakdown, lifetime net) + balance incl. converted opening balances, upcoming 14 days, recent, period selector
- [x] UI: Transactions, Categories ("what the AI sees"), Recurring, Import wizard, Dashboard charts (Chart.js), global Add expense/income modal (live FX preview, suggestions, repeat, shortcut `a`); Alpine components in static JS (no inline scripts)
- [x] Tests (202 total): schedule math (+hypothesis), parser (20), ledger service (13), import (8), ledger API (5), IDOR cases for transactions/categories/recurring/imports, page rendering + CSP script audit, RLS shared-system-rows; **Playwright e2e** (`make e2e`) passes with 0 console errors on the live stack
- [x] Phase 3 hooks left in place: LLM categorization of uncategorized rows after import/manual entry; "Load sample data" (both delivered in Phase 3)

### Phase 3 — Synthetic data + categorizer + eval infra (P0)  `[x]`
- [x] Rule lexicon `config/rules_lexicon.yml` (IN + US merchants/rails, sign-aware, accept ≥ 0.85) + matcher; normalizer hardened to **64 real-world descriptors** (tests); `config/thresholds.yml`
- [x] Categorizer agent (`finpilot/agents/categorizer.py`): memory → user keywords → lexicon → LLM via Mesh over unique merchants (batches ≤ 25, deterministic order), per-user enum validation, **flow-direction guard** (blocks injection like "debit → Salary"), < 0.6 → review, confident answers → merchant memory, degraded mode = zero LLM calls
- [x] Prompt `prompts/categorizer/classify.v1.md` (STATIC taxonomy+rules first, all user data in `<data>`); verified provider prefix caching through Mesh (25% of prompt tokens cached)
- [x] Redis L1 LLM cache (`cache:llm:{scope}:…`, user-scoped in product, persona-scoped in evals) → evals exactly reproducible; cache hits record the cost they saved
- [x] Instant rules+memory+keywords on write (manual, import, sample data); LLM pass as Celery `categorize_pending` (after import / uncategorized manual entry, per-user lock)
- [x] Synthetic generator (`eval/synth/`): 6 seeded personas (USD stable, USD freelancer, INR EMI+SIP, INR overspender+sub-creep+custom categories+injections, INR with USD subscriptions, USD with INR rent/remittance), 15 months (3 held out), 147 merchants (**39% unseen by rules**, ~15% ambiguous txns), typos, ground-truth series + injected anomalies, one global synthetic FX series (`fx_rates.scope='synthetic'`, never visible to real users)
- [x] Migration 0004 (eval tables, FX scope) and 0005 (first-admin rule ignores demo accounts)
- [x] Eval infra: loader (flagged synthetic users, auto-cleanup), categorization suite (layer ablation, per-category P/R/F1, confusions, calibration, per persona/currency, unseen/ambiguous, injection, custom categories, cost), runner with gates (`config/thresholds.yml`, exit code for CI), Postgres store, deterministic insights, Langfuse datasets + experiment runs with trace links, `eval/report.py` → `eval/results/RESULTS.md`
- [x] **First real numbers** (full, gpt-4o-mini via Mesh): accuracy 87.4–89.2% (3 fresh samplings), macro-F1 0.83–0.87, coverage 96%, unseen-merchant 70–78%, custom-category recall 96–100%, injection-safe 100%, $0.0013 per 1k transactions; offline precision-when-answering 99.0% at 68% coverage
- [x] Evaluation screen v1 (`/app/eval`): run selector, KPIs with deltas, layer ablation, calibration + persona charts, per-category table, confusions, insights, failures explorer with trace links, admin run buttons; API `/eval/runs|runs/{id}|runs/{id}/cases|compare|run`; nightly offline eval on beat
- [x] "Load sample data" (undoable import batch + declared recurring items) and `make seed-demo` (demo-inr@ / demo-usd@finpilot.local)
- [x] **Bug fixed: commit-after-response race** (1/40 stale reads after writes) → `get_db` is `scope="function"` everywhere + meta-test; 0/200 after fix
- [x] Tests 287 + e2e (sample data, Quality page) green

### Phase 4 — Recurring detection + analyzer + suites (P0; `fx_exposure` P1)  `[x]`
- [x] Migration 0006: `detected_recurrences` (series_key, period, amounts/CV, next_expected, status active/lapsed/gone, price history, usage_rating 0–5, promoted_to_rule_id, dismissed), `analysis_findings` (stable fingerprints, metric/baseline/observed/threshold, evidence ids, active/dismissed), `insight_snapshots`. RLS on all three; **no data_version triggers** (derived data)
- [x] Recurring detection `finpilot/recurring/detect.py` + `db/sql/recurring_candidates.sql`: fuzzy same-payee merge, period fit weekly→yearly (≥3 charges, ≥2 yearly, regularity ≥70%, CV, extra-charge share), amount clusters, price history and hikes, lapsed status, next due date; declared rules override (name or amount + period); promote / dismiss / restore; normalizer strips billing months and booking codes
- [x] Analyzer `finpilot/agents/analyzer.py` → typed `AnalysisReport` (`finpilot/insights/models.py`): anomalies (robust-z outliers, duplicates, first-time large, price hikes, category spikes), MoM and trailing-3 trends, patterns (weekend, payday, impulse proxy, concentration, savings-rate trend, latte factor), subscription audit (overlap / price_hike / low_value_proxy; never "unused"), **`fx_exposure`** (exact price vs FX split). Thresholds in `config/thresholds.yml`, `currencies.yml`, `config/subscriptions.yml`; text in `templates/messages/findings.yml`; persisted findings + snapshot under an advisory lock; Celery `refresh_analysis`
- [x] API: `GET /insights`, `POST /insights/findings/{id}/dismiss|restore`, `GET /subscriptions`, `POST /subscriptions/{id}/usage`, `GET /recurring/detected`, `POST /recurring/detected/{id}/promote|dismiss|restore`
- [x] UI: Insights page (anomaly cards with evidence links, subscription audit with usage ratings, patterns, weekly and savings charts, category trends, currency exposure); dashboard key insights + expected bills in "Next 14 days"; Recurring "We noticed these repeat"; Quality page anomaly/recurring section; transactions `?ids=` evidence filter
- [x] Eval suites 3 (anomalies, with clean-persona false alarms) and 4 (recurring + materializer idempotency/catch-up) with gates; generator posts weekend bank debits on Monday; regression insights compare like with like
- [x] Real numbers (offline, deterministic): anomalies P 56% / R 86% (material) / 0.20 false alarms per month; recurring P 96% / R 100% / period 100% / next date ±3 d 100% / amount error 2.7%; materializer 100% correct and idempotent
- [x] Tests 319 (+ property test that caught two next-date bugs) + e2e (Insights, promote a detected repeat) green
- [x] Committed `c67cdad`; RESULTS.md regenerated from the real offline + full runs (`47ae980`), all 12 offline / 12 full gates pass, Langfuse experiment runs for all 3 suites
- Decisions and deviations (details in DESIGN_NOTES §6):
  - Monthly period tolerance ±6 days (not ±4): real posting delays + February; costs one extra false positive (recurring precision 98.2% → 96.4%)
  - Subscription = `subscriptions`/`entertainment` category, or a fixed-price (CV ≤ 0.10) monthly/quarterly/yearly discretionary charge (train tickets every 2 weeks are not subscriptions)
  - `transactions.detected_recurrence_id` keeps its FK but stays unset: the analyzer never writes `transactions` (would bump data_version); evidence lives in `detected_recurrences.txn_ids_json`
  - "Load sample data" now keeps the planted duplicates/spikes (prompt-injection probes still excluded) so Insights has something to show
  - Anomaly `min_ratio` 2.0 → 3.0 after inspecting first-run false positives on the eval personas (disclosed in RESULTS.md); anomaly suite uses ground-truth categories
  - Analyzer narration by an LLM moved to Phase 6 (needs the grounding guard); Phase 4 text is template-only
- Follow-ups (not blocking):
  - [-] (deferred → README roadmap) Single-charge spike detection is the weak spot (recall 57%, precision 31%): high-variance merchants (Amazon, Walmart) need a per-merchant dispersion-aware threshold; re-measure on held-out personas before tuning again
  - [x] Add a held-out persona set (new seeds) so analyzer thresholds are not judged on the data they were tuned on (Phase 12: precision 0.53, recall 0.78, 0.28 false alarms/month, within the Phase 4 bounds)

### Phase 5 — Forecaster + backtest (P0), simulator + property tests + NL parse w/ interrupt/resume (P0); FX volatility perturbation (P1); NL amounts in any currency  `[x]`
Forecaster (`finpilot/forecasting/`, agent wrapper in `service.py`):
- [x] Migration 0007: `goals` (savings_target | spend_cap; RLS; data_version triggers) and `forecast_runs` (baseline/scenario results cited by id; RLS; derived → no triggers)
- [x] Inputs: start balance as of the day (dashboard `balance(as_of)`), declared rules ∪ active undeclared detected series (declared wins; learned business-day posting), incomes; foreign items at the latest STORED rate
- [x] Variable spend per category: same-weekday historical-day bootstrap (keeps seasonality and cross-category correlation), EW recency (half-life 45 d), payday effect (exactly expectation-preserving)
- [x] Monte Carlo 2,000 seeded paths with common random numbers; scheduled CV; (P1) FX random walk from stored rate returns; parameter uncertainty (bootstrap level) → p10/p50/p90, month ends, P(< 0), P(< cushion), projected monthly savings
- [x] Sparse data: low-confidence flag + 1.5× wider band + UI banner, never an error
- [x] Naive baselines (trailing mean, last-month replay); goal projection (savings ETA/shortfall/required monthly; spend-cap projected vs cap)
Simulator:
- [x] `Scenario` discriminated union (7 types + composites); amounts in any supported currency with the rate reported
- [x] Same-seed re-run → min balance + date, end deltas, Δ P(overdraft), verdict rule, top-2 levers each verified by re-simulation
- [x] `hypothesis` properties (purchase never raises, delta = purchase, cancel = charge × occurrences, same seed identical, composite = sum of parts) — pure tests + 282 checks on persona data in the eval
- [x] NL: `prompts/simulator_parser/parse.v2.md` → schema-validated parse → code decides missing slots → LangGraph `interrupt()` clarification resumed from the Redis checkpointer → simulation → templated answer (LLM narration behind the grounding guard: Phase 6); no Mesh → 503 + form
API / UI:
- [x] `GET /forecast`, `POST /simulate`, `POST /simulate/ask` + `/resume`, `GET/POST/PATCH/DELETE /goals` (+ IDOR case)
- [x] Forecast & what-if page (band chart with cushion, KPIs, month-end table, coming-up items, usual spending, NL box with clarifications, structured form, scenario overlay, verdict badge, verified levers, goals); dashboard projected month-end card + low-confidence banner; Quality page forecast/simulation section with predicted-vs-actual chart
Evaluation:
- [x] Suite 2 forecast backtest (6 personas × 4 rolling origins × 90 d): daily band coverage 77% (target 80%), 90-day coverage 79%, skill +62% vs trailing mean, +42% vs last month, sparse ok; gates
- [x] Suite 5 simulation: 282/282 property checks; historical one-off: reality inside the scenario band ~59% of days vs 0% for the baseline; NL (Mesh, full): 100% intent/slots/clarification on 18 questions with prompt v2 (v1: 94% intent, 89% fully right — v2 was revised on this set, so optimistic)
- [x] Tests 339 + e2e (what-if form, goal) green; reruns reproducible (persona seeds, stable item keys)
- Decisions / deviations (DESIGN_NOTES §7): Redis Search indexes only on DB 0 → checkpoints in DB 0 isolated by key prefix; parameter-uncertainty bootstrap added after the first backtest (72% → 77% daily coverage); sample data drops credit-card bill payments (double counting)
- Follow-ups:
  - [-] (deferred → README roadmap) Card spending vs card bill payments: a real "accounts" model (card account + transfers) instead of one balance (Phase 11 or later)
  - [x] LLM narration of simulation answers behind the grounding guard (done in Phase 6: `trail/narration.py`)

### Phase 6 — Advisor + grounding guard (incl. FX facts) + decision trail + advisor suite (P0); LLM narration of analyzer findings and simulator answers behind the guard  `[x]`
Grounding guard (`finpilot/trail/grounding.py`):
- [x] Extraction of amounts (₹/Rs/INR/$/USD prefix or suffix, Indian/Western grouping, lakh/crore/k, signs), percentages, numbers, ISO/written dates and months; currency-aware verification against a code-built FactSet (±1% / ±1 currency unit / written precision); converted figures pass only if they are facts (rate + rate date included); digits inside known names masked
- [x] `guarded_json`: generate → verify + validate → regenerate with problems named (≤ 2 retries, cacheable) → template fallback; report + Langfuse scores `grounding_pass`, `grounded_number_ratio`
Advisor:
- [x] Migration 0008: `recommendations` (+ candidate_key, kind, why, generated_by, data_version, feedback; status proposed/accepted/dismissed/superseded), `decision_trail` (+ candidate snapshot); RLS; finding kinds `forecast` / `goal` owned by the advisor
- [x] 9 deterministic candidate kinds with simulated effects, finding ids, evidence ids and named entities; analyzer adds `fees_paid` and `recurring_not_declared`; advisor adds `forecast_shortfall`, `goal_gap`, `planned_purchase_risk`
- [x] Smart LLM (`prompts/advisor/recommend.v1.md`) picks the top 3 from opaque ids + facts; validator (known id, cites its finding, names its entity, numbers are facts of THIS candidate) → retries → `templates/messages/advice.yml`; degraded mode = templates
- [x] Dismissed advice remembered by key for 90 days, reasons passed to the prompt; dismissal of a superseded card still counts
- [x] `recommendations` + `decision_trail` (trigger, findings, evidence, forecast_runs ref + before/after, model, prompt version, grounding report, Langfuse trace id via `agent_span`); Celery `refresh_advice` once per data_version
Narration behind the guard:
- [x] What-if answers (`simulator_parser/verbalize.v1.md`) and Insights summary (`analyzer_narrator/summary.v1.md`, cached per data_version); templates when the guard fails or Mesh is down
API / UI:
- [x] `GET /recommendations`, `POST /recommendations/refresh`, `POST /recommendations/{id}/accept|dismiss|feedback`, `GET /recommendations/{id}/trail`, `POST /insights/summary` (+ IDOR case)
- [x] Advice page (impact, accept/dismiss with reason, 👍/👎 → Langfuse `user_feedback`, "Why?" trail with findings, evidence, simulated before/after, model/prompt, grounding report, Langfuse link for admins); dashboard top 3 with "Why?"; Insights summary; header fits 9 sections (menu below lg)
Evaluation (suite 6):
- [x] Offline (templates): 18/18 recommendations cited, specific, grounded; 6/6 personas with their own top advice; dismissed never repeated; guard caught 100% of 40 corruptions, 0 false alarms
- [x] Full (gpt-4o via Mesh, committed run at d2b567a): 100% cited / specific / grounded, first-attempt grounding 83%, fallback 0%, judge 4.8/5 (0.97), guard caught 100% of corruptions (an earlier run: 39/40, coincidental match, documented), dismissed never repeated
- [x] Tests 386 + e2e (advice, trail, dismiss, summary) green
- Decisions / deviations (DESIGN_NOTES §8): scoped facts per recommendation; advisor eval personas get an opening balance; subscription heuristic limited to the user's own categories (remittance was offered as a subscription)
- Follow-ups:
  - [-] (deferred → README roadmap) A semantic check on top of number matching (which fact a number claims to be), if the guard's coincidental-match rate matters in practice
  - [x] Goal tracker snapshots/alerts (done in Phase 7; weekly deltas also in digests, Phase 10)

### Phase 7 — LangGraph coordinator on Redis checkpointer + agent_runs + goal tracker + degraded mode (P0/P1)  `[x]`
- [x] Migration 0009: `pipeline_runs`, `agent_runs` (identity bigint, one row per node), `goal_snapshots` (unique per goal + ISO week); goals gain `debt_payoff`; RLS on all
- [x] Coordinator: `route_intent → ingest → [categorize] → analyze → forecast → goals → advise → persist_trail → snapshot → [deliver]` on the Redis checkpointer (thread `user:{id}:run:{run_id}`); conditional edges with recorded skip reasons; `explain` and `ask_affordability` intents through the same graph (cache/chat intents: Phases 8/9)
- [x] One DB transaction per node → crash → `resume_run` re-executes only the failed node onward (tested); `agent_runs` + Redis `run:{id}` progress + Langfuse span per node; run score `degraded`
- [x] Degraded mode (Mesh unconfigured, or any LLM failure mid-run) → rules-only categorization, template advice/narration, zero further LLM calls (tested: exactly 1 failing call per run); fixed the categorizer swallowing Mesh outages per batch
- [x] Goal tracker: weekly snapshots with deltas; `goal_ahead` finding + `goal_surplus` candidate; `debt_payoff` projection (loan category pace) + `debt_catch_up` candidate
- [x] Celery `run_pipeline` (per-user lock), `schedule_pipeline` (90 s debounce), `resume_stale_runs` on worker start; replaces the categorize → analysis → advice chain
- [x] API: `POST /pipeline/run`, `GET /pipeline/runs`, `GET /pipeline/runs/{id}`, `GET /pipeline/runs/{id}/events` (SSE), `POST /pipeline/runs/{id}/resume`, `GET /goals/{id}/snapshots` (+ IDOR cases)
- [x] UI: Pipeline Trace (timeline in pipeline order, skip reasons, degraded badge, Resume, live SSE progress, Langfuse link for admins); dashboard degraded notice; debt goals + weekly deltas; nav from 1280 px with menu below
- [x] Tests 393 + e2e (Run now → live → timeline) green; a live run on the stack: 7 nodes in ~2 s with the LLM advisor first-try grounded
- Follow-ups:
  - [x] Daily `refresh_insights` beat tick per active user and digest delivery in `deliver` (Phase 10)
  - [x] Pre-warm answer caches in `snapshot` (Phase 8)

### Phase 8 — Caching L0–L3 + intent router + snapshots + pre-warm + cache suite (P0); `fx_asof`/`reporting_currency` in keys  `[x]`
- [x] Intent router (`config/intents.yml`, `finpilot/agents/intent_router.py`): 11 intents (spending_trend, where_to_save, where_overspending, top_categories, upcoming_bills, subscription_review, goal_status, savings_rate, income_summary, afford_purchase, free_form) with handler / ttl / time_relative / cacheable / period slot; match order rules → embedding similarity to example phrasings (if embeddings work) → fast-LLM classification (`prompts/intent_router/classify.v1.md`)
- [x] Answer engine (`finpilot/answers/`): facts from deterministic sources (dashboard aggregates, analysis report, forecast, recommendations); `deterministic_template` intents need no LLM (`templates/messages/answers.yml`); `llm_from_facts` intents phrase the facts under the grounding guard (`prompts/answers/answer.v1.md`) with the template as fallback; afford_purchase → simulator (never cached)
- [x] L2 answer cache `cache:ans:{user}:{intent}:{period}:{data_version}:{currency}:{locale}:{prompt_version}:{model}[:{date bucket}][:fx {fx_asof}]` (version-based invalidation, no scanning); value `{answer, facts, grounding, created_at}`; never cache degraded or user-amount answers; single-flight `cache:lock:` so concurrent misses compute once
- [x] L3 semantic cache (migration 0010 `semantic_cache`, pgvector HNSW, RLS): same user + intent + period + data_version + currency, cosine ≥ 0.92 (config); disabled without embeddings
- [x] L0 provider prefix caching and L1 already in place (Phases 0/3): STATIC-first prompts, `cached_tokens` recorded; the new prompts follow the same layout
- [x] Pre-warming: the pipeline's snapshot node computes the top intents into L2 + `insight_snapshots(kind='answers')`; at login if stale
- [x] Observability: `answer_log` (intent, period, cache_layer, latency, generated_by; RLS), Langfuse `cache_hit` score; Evaluation screen: hit rate and p50/p95 by layer
- [x] API `GET /answers?intent=&period=`, `POST /answers {question}`; dashboard v2: instant chips ("How is my spending trend?", "Where can I save more?", "Where am I spending the most?", "What's coming up?") + ask box with "instant answer · updated 2 h ago" badge and Refresh
- [x] Eval suite 7 (cache & latency): 200-question seeded session mix with paraphrases, two writes mid-session (next answer must miss), a second user (no cross-user hits), hit rate by layer, p50/p95 by layer, cost saved, router accuracy; gates (cached p95 < 300 ms)
- [x] Tests (key composition incl. currency / fx_asof / date bucket, invalidation on data_version, single-flight, cross-user isolation, degraded not cached, router layers, semantic cache), e2e (chips), docs, commit
### Phase 9 — Memory (short/long) + chat agent (incl. `convert_currency` tool) + memory page + suite (P1)  `[x]`
- [x] Migration 0011: `chat_sessions` (summary, last_message_at, consolidated_at), `chat_messages` (role, content, tool trace, grounding_json, cache_layer, feedback, langfuse_trace_id), `memories` (kind, topic, text, embedding vector(1536) NULL + HNSW, tsvector + trigram for the lexical fallback, importance, confidence, pinned, source_ref, last_used_at, expires_at, superseded_by); all RLS, no data_version triggers (not financial facts)
- [x] Short-term memory (`finpilot/memory/short_term.py`): Redis `chat:{user}:{session}` last 12 turns, 24 h TTL, rebuilt from `chat_messages` on a miss; rolling summary (fast LLM, `prompts/chat/summarize.v1.md`; truncation fallback) beyond the token budget
- [x] Long-term memory (`finpilot/memory/long_term.py`): `SemanticMemory` protocol + pgvector implementation; lexical fallback (FTS + trigram) without embeddings; writes never store account/card numbers or credentials (config/memory.yml filters)
- [x] Retrieval (`retrieval.py`): 0.6·similarity + 0.25·recency + 0.15·importance (pinned first), top-k 5 within a token budget, rendered into the DYNAMIC section inside `<memory>` tags
- [x] Writer (`writer.py`, `prompts/memory_writer/extract.v1.md`, fast model, schema): candidates with kind/topic/importance/confidence/expiry; dedupe (similarity > 0.9 → merge), same kind+topic with different content → supersede, expiry for time-bound facts; Celery `memory_consolidation` tick (sessions idle 15 min); deterministic decision memories on recommendation accept/dismiss
- [x] Advisor receives the user's relevant preference/decision memories (`advisor/recommend.v2`)
- [x] Chat agent (`finpilot/agents/chat_agent.py`): canonical questions (router rules/embedding) → answer cache (L2/L3); afford → simulator (clarification resumes across turns); otherwise a Mesh tool loop (≤ 6 iterations) over `query_transactions`, `get_category_trend`, `get_forecast`, `simulate`, `list_subscriptions`, `get_goal_status`, `list_recurring`, `explain_recommendation`, `recall_memory`, `convert_currency` (FX service); tool output inside `<data>`; final answer through the grounding guard (facts = tool outputs) with a deterministic fallback; tool-agent answers not cached
- [x] Coordinator `answer_from_cache` and `free_chat` nodes
- [x] API: `POST /chat` (JSON) and `POST /chat/stream` (SSE: tool progress, then the verified message), `GET /chat/sessions`, `GET /chat/sessions/{id}`, `POST /chat/messages/{id}/feedback` (Langfuse `user_feedback`), `GET/DELETE /memory`, `PATCH/DELETE /memory/{id}`; chat rate limit
- [x] UI: Chat page (sessions, suggested questions, progress lines, cache badge, 👍/👎), Memory page (edit, pin, delete, wipe all)
- [x] Eval suite 8 (memory): retrieval hit@k on synthetic fact/question pairs (offline lexical, full embeddings), supersede correctness, deletion removes recall, writer extraction on synthetic chats (full); chat grounding on a small question set (full)
- [x] Tests (memory write/dedupe/supersede/retrieve/delete, short-term, chat tool loop + guard + fallback, convert_currency, IDOR for sessions/memories), e2e (chat + memory), docs, commit
### Phase 10 — Scheduler ticks, digests, risk alerts, `refresh_fx_rates` (2×/day + on stale startup), adapters (P0/P1)  `[x]`
- [x] Migration 0012: `digests` (UNIQUE (user_id, period_key), channel, status, subject/body, payload, degraded, generated_by, grounding), `alerts` (kind, dedupe_key, severity, payload, delivery, read_at), `telegram_link_tokens` (hashed, single use, expiring), `job_runs` (identity bigint: task, status, timings, result) — RLS on user tables
- [x] Digest slot logic (`finpilot/digests/schedule.py`): the user's preferred weekday/hour in their timezone; a digest is due from the slot for 24 h (catch-up after downtime), period_key = local slot date; idempotent via UNIQUE + `lock:digest:` Redis lock
- [x] Digest content (`finpilot/digests/service.py`): ONE key insight, ONE action with its simulated effect, goal status line (+ delta since last week), bills in the next 14 days, degraded and FX notices; fast-LLM phrasing of the insight/action sentences only (`prompts/digest/phrase.v1.md`, grounded, template fallback); text in `templates/messages/digest.txt.j2`
- [x] Channels: in-app (always stored), email (SMTP or log sink), Telegram (Bot API or log sink); deep-link linking `t.me/<bot>?start=<token>` + webhook (secret path) and an optional polling task, both binding `chat_id`; unlinked Telegram → in-app with a note
- [x] Ticks (beat): `digests_due` 15 min → pipeline run (trigger `schedule`) whose `deliver` node sends the due digest from fresh data; hourly `risk_alerts` (forecast p10 < buffer within 14 days, 7-day dedupe); daily `refresh_insights` for active users (+ pre-warm); weekly `goal_snapshots`; nightly `cache_gc` (semantic cache of old data versions, old answer/job logs, outbox files); `refresh_fx_rates` + `memory_consolidation` already in place
- [x] `job_runs` recorded from Celery signals; `GET /scheduler/jobs` (admin: beat entries, last/next run, recent results) + admin section on the Pipeline page; `POST /digest/preview` (admin, labelled preview)
- [x] API + UI: `GET /digests`, `GET /digests/{id}`, `GET /alerts`, `POST /alerts/{id}/read`, `POST /settings/telegram/link`, `DELETE /settings/telegram`; Inbox page (digest history + alerts) with an unread badge in the header; dashboard low-balance alert banner; Settings: channel + Telegram linking
- [x] Tests (slot math across timezones/DST, idempotency, content, grounding + fallback, channels via log sink and respx, Telegram linking/webhook secret, alert dedupe, ticks select the right users, job_runs, admin-only scheduler, IDOR for digests/alerts), e2e (inbox, settings), docs, commit
### Phase 11 — UI completion, Evaluation screen (insights/failures/compare, FX & MCP tab), a11y (P0/P1)  `[x]`
- [x] App shell (user request): left sidebar with grouped navigation (Overview · Money ▸ Transactions/Recurring/Categories · Planning ▸ Forecast & goals/Advice/Insights · Assistant ▸ Chat/Memory; Tools: Import, Pipeline, Quality, Settings), "+ Add" menu, top bar (currency, theme, inbox bell, user menu); mobile drawer; same bluish palette
- [x] Theme-aware semantic colours (`text-muted`, `text-accent`, `text-pos`, `text-neg`, `text-warn`) defined once as CSS variables for light and dark, replacing per-template slate/brand/emerald/rose text shades; Inter actually loaded (pinned @fontsource-variable/inter), tabular numbers for amounts
- [x] `make a11y`: axe-core (WCAG 2.1 A/AA) on every page in light AND dark mode; baseline 838 violations (640 dark-mode contrast) → 0; focusable scroll regions, underlined in-text links
- [x] Dashboard quick-add row (expense, income, import, ask)
- [x] Evaluation screen: suite tabs, run history trend charts, compare-two-runs view, insights panel, failures explorer per suite, "FX & MCP" tab
- [x] Eval suite 9 (FX & MCP, AMENDMENT_1): conversion vs a fixed reference table, INR↔USD round trip, weekend/holiday dates, fallback chain under MCP outage / HTTP outage / stale DB (source + UI flag), MCP contract tests against the FX server, allowlist enforcement, circuit breaker open/recover, grounding of converted numbers, fx_exposure correctness
- [x] Deferred from Phase 1: `GET /me/export` (JSON of everything the user owns), `DELETE /me` (password-confirmed, cascade) + Settings "Your data" card
- [x] Tests, e2e (sidebar navigation, theme toggle), a11y 0 violations, docs, commit
### Phase 12 — Full eval run incl. FX suite, README/DESIGN_NOTES with real numbers, Caddy prod compose + DEPLOY.md (incl. db-backup/restore), cleanup (P0)  `[x]`
- [x] Held-out personas (new seeds, never inspected) for the anomaly suite, reported next to the tuned set, so the threshold tuned in Phase 4 is judged on unseen data
- [x] `docker-compose.prod.yml` + `deploy/caddy/Caddyfile` (profile `prod`: automatic HTTPS for `APP_DOMAIN`, app and optional Langfuse behind it, no published DB/Redis ports, secure cookies) + DEPLOY.md (VPS, Render/Fly notes, secrets, backups); verified locally with Caddy's internal CA
- [x] `make db-backup` / `make db-restore` verified end to end (restore into a scratch database)
- [x] Definition-of-done checks on the running stack: fx-mcp stopped → app works and shows approximate/stale rates; a USD expense by an INR user shows both amounts in INR and USD views
- [x] Cleanup: ruff, mypy on `finpilot mcp_servers eval` (as CI), dead-code scan, secrets scan of the working tree and git history, `.env` ignored, stray files
- [x] README rewrite (what it is, Mermaid architecture + LangGraph flow, agent contracts, ≤ 5-command quickstart, local dev, Langfuse self-hosted/cloud, feature tour, API list, Mesh usage, real evaluation numbers, limitations, roadmap); DESIGN_NOTES decisions index + "DATA MODEL (PostgreSQL schema)"
- [x] Final full eval run (offline + Mesh) on the final commit; RESULTS.md committed; README/DESIGN_NOTES numbers from that run

---

### Iteration 1 (after Phase 12, user request) — home screen, demo accounts, committed sample data, admin-only tools  `[x]`
- [x] Dashboard: drop the "Ask FinPilot" box (the quick-add tile opens Chat) and "Key insights"; one-line dismissable low-balance alert; period selector as a dropdown; trend chart starts at the first active month
- [x] Sample data committed in `data/sample/` (INR only, USD only, INR + USD reporting INR; 2026-04 → 2026-10, plain descriptions, all categorized, planted Insights events); "Load sample data" with a dataset choice on the dashboard (new/small accounts), Import page and *Add* menu; undo also removes its recurring items
- [x] `make seed-demo`: admin@ (no data), inr@, usd@, mixed@finpilot.local; refuses the default password in prod
- [x] Categories page compact (two columns, one line per category, actions in a menu)
- [x] Pipeline and Quality visible to admins only (sidebar + 404 for others); e2e/a11y use the seeded admin
- [x] Tests (`tests/test_sample_data.py`), e2e 0 errors, a11y 0 violations; DESIGN_NOTES §15

## 4. Risks

| Risk | Mitigation |
|---|---|
| Mesh `/embeddings` unavailable | Phase 0 probe; lexical fallback (FTS + pg_trgm), `EMBEDDINGS_ENABLED=false`, documented |
| No `MESH_API_KEY` in the dev environment yet | Recorded-response fake for tests; live probe script `scripts/mesh_probe.py` run once key provided |
| Mesh doesn't pass `cached_tokens` | Record what's observed; store NULL, don't fabricate |
| Langfuse self-host stack is heavy (ClickHouse, MinIO) | `include:` so it can be skipped; Cloud mode documented; app no-ops if unreachable |
| LangGraph Redis checkpointer needs Redis modules | Use Redis 8 (built-in query engine/JSON) per verified docs |
| FX source drops INR / rate-limits | Fallback chain + pinned YAML; provider module swappable; evals use synthetic FX series (no network) |
| RLS + SQLAlchemy pooling leaks `app.user_id` | `SET LOCAL` inside an explicit transaction only; test for leakage across pooled connections |
| Scope is very large for a hackathon | Strict P0-first; P2 (OAuth, Langfuse-side evaluators) cut first |

## 5. Open questions (non-blocking; defaults chosen)
1. Mesh key — needed for live probe/smoke & `make eval`. Default: build against the fake, run probe when provided.
2. Repo dir is `finplot`, product/package is `finpilot`. Default: keep `finpilot` package name.
3. Telegram bot token/SMTP creds — default to `log_sink` delivery.
