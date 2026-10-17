# FinPilot evaluation results

Generated from Postgres (`eval_runs` / `eval_metrics`) by `python -m eval.report`. Do not edit by hand. Personas are seeded synthetic INR/USD users with ground truth (see DESIGN_NOTES.md § Evaluation).

## Full run (rules + memory + LLM via Mesh)

- Run `a73cc653-f116-4a1c-8a8f-e6d5a74b31c6` · 2026-10-17 02:23 UTC · commit `f4c1a97` · status **passed**
- Model: `openai/gpt-4o-mini` · prompt `categorizer/classify@v1#af8129b98b4c`

### Categorization: layer ablation

| Variant | Accuracy | Coverage | Precision when answering | Macro-F1 |
|---|---:|---:|---:|---:|
| Rules only | 64.2% | 64.8% | 98.9% | 0.687 |
| + merchant memory | 67.2% | 67.9% | 99.0% | 0.696 |
| + LLM (Mesh) | 87.4% | 95.6% | 91.3% | 0.841 |

| Metric (primary variant) | Value |
|---|---:|
| Unseen-merchant accuracy | 71.2% |
| Ambiguous-merchant accuracy | 35.5% |
| Custom-category recall | 100.0% |
| Prompt-injection safe | 100.0% |
| Scored transactions | 4091 |
| LLM calls per 1k transactions | 1.96 |
| LLM cost per 1k transactions (uncached run) | $0.0012 |
| LLM spend per 1k this run (after L1 cache) | $0.0000 |

| Persona | Accuracy |
|---|---:|
| inr_overspender_subcreep | 86.9% |
| inr_salaried_emi_sip | 92.9% |
| inr_with_usd_subscriptions | 93.4% |
| usd_freelancer_irregular | 78.2% |
| usd_salaried_stable | 90.3% |
| usd_with_inr_rent_remittance | 77.3% |

Weakest categories (F1 < 0.70): education (0.00, n=11), impulse_misc (0.00, n=28), other (0.50, n=172), freelance (0.56, n=23), shopping (0.60, n=367), transfers (0.65, n=108).

### Anomaly detection (deterministic, injected ground truth)

| Metric | Value |
|---|---:|
| Precision | 56.2% |
| Recall on material anomalies | 85.7% |
| Recall on all injected anomalies | 60.0% |
| F1 | 0.679 |
| False alarms per month on clean personas | 0.20 |
| Held-out personas (new seeds): precision / recall / false alarms per clean month | 52.9% / 78.3% / 0.28 |

| Type | Injected (material) | Found | Precision | Recall |
|---|---:|---:|---:|---:|
| duplicate | 13 (10) | 10 | 71.4% | 100.0% |
| large one off | 2 (2) | 2 | 66.7% | 100.0% |
| price hike | 2 (2) | 2 | 100.0% | 100.0% |
| spike | 13 (7) | 4 | 30.8% | 57.1% |

### Recurring detection and materializer

| Metric | Value |
|---|---:|
| Precision | 96.4% |
| Recall | 100.0% |
| Period accuracy | 100.0% |
| Amount error (MAPE vs nominal price) | 2.7% |
| Next-date error (mean days) | 0.22 |
| Next date within ±3 days | 100.0% |
| Materializer correct (month-end, biweekly, quarterly, catch-up) | 100.0% |
| Materializer idempotent (repeat ticks create nothing) | 100.0% |

### Forecast backtest (24 rolling origins × 90 days, ground-truth categories)

| Horizon | Error: model | Trailing-mean baseline | Last-month baseline | Actual inside p10–p90 |
|---|---:|---:|---:|---:|
| 30 days | 0.25 | 0.46 | 0.40 | 75.0% |
| 60 days | 0.44 | 0.86 | 0.74 | 75.0% |
| 90 days | 0.65 | 1.22 | 0.97 | 79.2% |

Error = |median forecast − actual balance| ÷ the persona's average monthly spend. Days inside the p10–p90 band: 77.0% (target ≈ 80%). Skill vs trailing mean 62.4%, vs last month 42.1% (share of error removed). Sparse-data handling: ok.

### What-if simulator

- Property checks: 282/282 held (same seed identical, delta equals purchase, purchase never raises, composite sum of parts, cancel raises by charges).
- Real one-off purchase (usd_salaried_stable, 2025-08-10): actual balance inside the band on 85.0% of days with the what-if vs 0.0% without it.
- Real one-off purchase (inr_salaried_emi_sip, 2025-08-10): actual balance inside the band on 33.3% of days with the what-if vs 0.0% without it.
- Natural-language parsing (18 questions, Mesh): intent 100.0%, slots 97.9%, asks for missing amounts 100.0%, fully right 94.4%. Prompt v2 was revised after v1 missed 2 of these questions, so this is optimistic.

### Advisor (smart LLM via Mesh + grounding guard)

| Metric | Value |
|---|---:|
| Recommendations cite ≥ 1 finding | 100.0% |
| Name a specific merchant / category / goal | 100.0% |
| Every number grounded in computed facts | 100.0% |
| Personas with a top recommendation of their own | 100.0% |
| Dismissed advice not repeated | 100.0% |
| Guard catches corrupted numbers (scaled, fabricated, wrong currency) | 100.0% |
| Grounding passes on the first attempt | 100.0% |
| Fell back to templates after 2 retries | 0.0% |
| LLM-as-judge overall (rubric prompts/eval_judge/advice.v1.md, 1–5 scaled to 0–1) | 87.8% |

### Answer cache (200-question session, writes at 70 and 140)

- Hit rate on cacheable answers 87.5%; invalidated after writes 100.0% (16 checks); cross-user isolation 100.0% (9 checks); degraded answers 0.0%.
- Router: 34/35 labelled phrasings (llm 2, rules 32, embedding 1).
- LLM cost saved by hits in the session: $0.0112.

| Layer | Answers | p50 ms | p95 ms |
|---|---:|---:|---:|
| L2 exact answer | 172 | 1 | 5 |
| L3 semantic | 3 | 665 | 782 |
| computed (miss) | 25 | 875 | 5293 |

### Memory (Mesh embeddings)

- Recall on 20 questions over 16 remembered facts: hit@1 90.0%, hit@5 95.0%.
- A newer fact replaces a contradicted one 100.0%; deleted memories never recalled again 100.0%; account numbers / credentials rejected 100.0%; other users recall nothing 100.0%.
- Memory writer on 4 synthetic chats: found 100.0% of the expected memories, 100.0% of what it stored was expected, stored no quoted figures or account numbers in 100.0% of chats.
- Chat on the sample data (10 questions; routes: cache 4, tools 4, fallback 1, simulator 1): every number verified in 90.0% of answers, fallback 10.0%, p50 2.6 s, p95 6.6 s. Grounded is not the same as relevant: answers are checked by hand in the failures explorer.

### FX & MCP (no network: in-process MCP server, fake HTTP)

| Check | Result |
|---|---:|
| Conversions exact vs reference table | 100.0% |
| USD → INR → USD within $0.01 | 100.0% |
| Weekend / holiday / future dates | 100.0% |
| Fallback chain: source and flags (MCP down, HTTP down, stale DB, pinned) | 100.0% |
| MCP contract (tools = allowlist, shapes, tool errors, caching) | 100.0% |
| Allowlist enforced before any I/O | 100.0% |
| Circuit breaker opens, half-opens, recovers | 100.0% |
| Wrong converted amounts caught by the guard | 100.0% |
| Correct converted amounts pass the guard | 100.0% |
| fx_exposure vs generator ground truth | 100.0% |

### Guardrails (deterministic)

| Check | Result |
|---|---:|
| Identifiers masked before Mesh / Langfuse | 100.0% |
| Amounts, dates, references wrongly masked | 0.0% |
| Answers with an identifier blocked | 100.0% |
| Clean answers passed | 100.0% |
| Investment / tax / legal directives flagged | 100.0% |
| FinPilot levers passed | 100.0% |
| Injected text kept inside <data> blocks | 100.0% |
| Masking p95 | 1.2 ms |

Gates: categorization.accuracy ✅, categorization.macro_f1 ✅, categorization.unseen_merchant_accuracy ✅, categorization.injection_safe ✅, categorization.custom_category_recall ✅, anomalies.recall ✅, anomalies.precision ✅, anomalies.fp_per_month_clean ✅, anomalies.heldout_recall ✅, anomalies.heldout_precision ✅, anomalies.heldout_fp_per_month_clean ✅, recurring.precision ✅, recurring.recall ✅, recurring.materializer_correct ✅, recurring.materializer_idempotent ✅, forecast.coverage_daily ✅, forecast.skill_vs_trailing_mean ✅, simulation.property_pass_rate ✅, simulation.nl_intent_accuracy ✅, simulation.nl_slot_accuracy ✅, advisor.pct_with_finding ✅, advisor.pct_specific ✅, advisor.pct_grounded ✅, advisor.dismissed_not_repeated ✅, advisor.guard_detection_rate ✅, advisor.grounding_first_attempt_pass_rate ✅, advisor.judge_overall ✅, cache.hit_rate ✅, cache.cached_p95_ms ✅, cache.invalidation_correct ✅, cache.cross_user_isolation ✅, cache.router_accuracy ✅, cache.degraded_share ✅, memory.hit_at_5 ✅, memory.scope_rules_false_refusal ✅, memory.scope_refusal_recall ✅, memory.scope_false_refusal ✅, memory.supersede_correct ✅, memory.deletion_removes_recall ✅, memory.blocked_rejected ✅, memory.cross_user_isolation ✅, memory.writer_recall ✅, memory.writer_no_leak ✅, memory.chat_grounded ✅, fx.conversion_accuracy ✅, fx.round_trip_within_0_01 ✅, fx.date_resolution ✅, fx.fallback_chain ✅, fx.mcp_contract ✅, fx.allowlist_enforced ✅, fx.circuit_breaker ✅, fx.grounding_converted_detection ✅, fx.grounding_converted_clean_pass ✅, fx.fx_exposure_correct ✅, guardrails.pii_mask_recall ✅, guardrails.pii_false_mask_rate ✅, guardrails.pii_mask_p95_ms ✅, guardrails.leak_detection_rate ✅, guardrails.leak_clean_pass_rate ✅, guardrails.directive_detection_rate ✅, guardrails.directive_clean_pass_rate ✅, guardrails.injection_contained ✅.

## Offline run (rules + memory, no LLM)

- Run `3140fcba-18b4-497e-b9ed-c2991adccd16` · 2026-10-17 02:22 UTC · commit `f4c1a97` · status **passed**
- Model: `openai/gpt-4o-mini` · prompt `categorizer/classify@v1#af8129b98b4c`

### Categorization: layer ablation

| Variant | Accuracy | Coverage | Precision when answering | Macro-F1 |
|---|---:|---:|---:|---:|
| Rules only | 64.2% | 64.8% | 98.9% | 0.687 |
| + merchant memory | 67.2% | 67.9% | 99.0% | 0.696 |

| Metric (primary variant) | Value |
|---|---:|
| Unseen-merchant accuracy | 0.0% |
| Ambiguous-merchant accuracy | 10.0% |
| Custom-category recall | 0.0% |
| Prompt-injection safe | 100.0% |
| Scored transactions | 4091 |

| Persona | Accuracy |
|---|---:|
| inr_overspender_subcreep | 71.1% |
| inr_salaried_emi_sip | 65.5% |
| inr_with_usd_subscriptions | 72.8% |
| usd_freelancer_irregular | 58.0% |
| usd_salaried_stable | 65.8% |
| usd_with_inr_rent_remittance | 67.5% |

Weakest categories (F1 < 0.70): Pet Care (0.00, n=29), Tuition (0.00, n=22), education (0.00, n=11), impulse_misc (0.00, n=28), other (0.07, n=172), transfers (0.36, n=108).

### Anomaly detection (deterministic, injected ground truth)

| Metric | Value |
|---|---:|
| Precision | 56.2% |
| Recall on material anomalies | 85.7% |
| Recall on all injected anomalies | 60.0% |
| F1 | 0.679 |
| False alarms per month on clean personas | 0.20 |
| Held-out personas (new seeds): precision / recall / false alarms per clean month | 52.9% / 78.3% / 0.28 |

| Type | Injected (material) | Found | Precision | Recall |
|---|---:|---:|---:|---:|
| duplicate | 13 (10) | 10 | 71.4% | 100.0% |
| large one off | 2 (2) | 2 | 66.7% | 100.0% |
| price hike | 2 (2) | 2 | 100.0% | 100.0% |
| spike | 13 (7) | 4 | 30.8% | 57.1% |

### Recurring detection and materializer

| Metric | Value |
|---|---:|
| Precision | 96.4% |
| Recall | 100.0% |
| Period accuracy | 100.0% |
| Amount error (MAPE vs nominal price) | 2.7% |
| Next-date error (mean days) | 0.22 |
| Next date within ±3 days | 100.0% |
| Materializer correct (month-end, biweekly, quarterly, catch-up) | 100.0% |
| Materializer idempotent (repeat ticks create nothing) | 100.0% |

### Forecast backtest (24 rolling origins × 90 days, ground-truth categories)

| Horizon | Error: model | Trailing-mean baseline | Last-month baseline | Actual inside p10–p90 |
|---|---:|---:|---:|---:|
| 30 days | 0.25 | 0.46 | 0.40 | 75.0% |
| 60 days | 0.44 | 0.86 | 0.74 | 75.0% |
| 90 days | 0.65 | 1.22 | 0.97 | 79.2% |

Error = |median forecast − actual balance| ÷ the persona's average monthly spend. Days inside the p10–p90 band: 77.0% (target ≈ 80%). Skill vs trailing mean 62.4%, vs last month 42.1% (share of error removed). Sparse-data handling: ok.

### What-if simulator

- Property checks: 282/282 held (same seed identical, delta equals purchase, purchase never raises, composite sum of parts, cancel raises by charges).
- Real one-off purchase (usd_salaried_stable, 2025-08-10): actual balance inside the band on 85.0% of days with the what-if vs 0.0% without it.
- Real one-off purchase (inr_salaried_emi_sip, 2025-08-10): actual balance inside the band on 33.3% of days with the what-if vs 0.0% without it.

### Advisor (deterministic templates)

| Metric | Value |
|---|---:|
| Recommendations cite ≥ 1 finding | 100.0% |
| Name a specific merchant / category / goal | 100.0% |
| Every number grounded in computed facts | 100.0% |
| Personas with a top recommendation of their own | 100.0% |
| Dismissed advice not repeated | 100.0% |
| Guard catches corrupted numbers (scaled, fabricated, wrong currency) | 100.0% |

### Answer cache (200-question session, writes at 70 and 140)

- Hit rate on cacheable answers 15.5%; invalidated after writes 100.0% (16 checks); cross-user isolation 100.0% (9 checks); degraded answers 77.5%.
- Router: 32/35 labelled phrasings (rules 32, default 3).

| Layer | Answers | p50 ms | p95 ms |
|---|---:|---:|---:|
| L2 exact answer | 31 | 1 | 2 |
| computed (miss) | 169 | 28 | 49 |

### Memory (lexical retrieval, no Mesh)

- Recall on 20 questions over 16 remembered facts: hit@1 50.0%, hit@5 70.0%.
- A newer fact replaces a contradicted one 100.0%; deleted memories never recalled again 100.0%; account numbers / credentials rejected 100.0%; other users recall nothing 100.0%.

### FX & MCP (no network: in-process MCP server, fake HTTP)

| Check | Result |
|---|---:|
| Conversions exact vs reference table | 100.0% |
| USD → INR → USD within $0.01 | 100.0% |
| Weekend / holiday / future dates | 100.0% |
| Fallback chain: source and flags (MCP down, HTTP down, stale DB, pinned) | 100.0% |
| MCP contract (tools = allowlist, shapes, tool errors, caching) | 100.0% |
| Allowlist enforced before any I/O | 100.0% |
| Circuit breaker opens, half-opens, recovers | 100.0% |
| Wrong converted amounts caught by the guard | 100.0% |
| Correct converted amounts pass the guard | 100.0% |
| fx_exposure vs generator ground truth | 100.0% |

### Guardrails (deterministic)

| Check | Result |
|---|---:|
| Identifiers masked before Mesh / Langfuse | 100.0% |
| Amounts, dates, references wrongly masked | 0.0% |
| Answers with an identifier blocked | 100.0% |
| Clean answers passed | 100.0% |
| Investment / tax / legal directives flagged | 100.0% |
| FinPilot levers passed | 100.0% |
| Injected text kept inside <data> blocks | 100.0% |
| Masking p95 | 1.4 ms |

Gates: categorization.precision_resolved ✅, categorization.coverage ✅, categorization.injection_safe ✅, anomalies.recall ✅, anomalies.precision ✅, anomalies.fp_per_month_clean ✅, anomalies.heldout_recall ✅, anomalies.heldout_precision ✅, anomalies.heldout_fp_per_month_clean ✅, recurring.precision ✅, recurring.recall ✅, recurring.period_accuracy ✅, recurring.next_date_within_3d ✅, recurring.materializer_correct ✅, recurring.materializer_idempotent ✅, forecast.coverage_daily ✅, forecast.skill_vs_trailing_mean ✅, forecast.skill_vs_last_month ✅, forecast.sparse_ok ✅, simulation.property_pass_rate ✅, simulation.historical_scenario_closer ✅, advisor.pct_with_finding ✅, advisor.pct_specific ✅, advisor.pct_grounded ✅, advisor.dismissed_not_repeated ✅, advisor.guard_detection_rate ✅, advisor.guard_clean_pass_rate ✅, cache.invalidation_correct ✅, cache.cross_user_isolation ✅, cache.cached_p95_ms ✅, cache.router_accuracy ✅, memory.hit_at_5 ✅, memory.scope_rules_false_refusal ✅, memory.supersede_correct ✅, memory.deletion_removes_recall ✅, memory.blocked_rejected ✅, memory.cross_user_isolation ✅, fx.conversion_accuracy ✅, fx.round_trip_within_0_01 ✅, fx.date_resolution ✅, fx.fallback_chain ✅, fx.mcp_contract ✅, fx.allowlist_enforced ✅, fx.circuit_breaker ✅, fx.grounding_converted_detection ✅, fx.grounding_converted_clean_pass ✅, fx.fx_exposure_correct ✅, guardrails.pii_mask_recall ✅, guardrails.pii_false_mask_rate ✅, guardrails.pii_mask_p95_ms ✅, guardrails.leak_detection_rate ✅, guardrails.leak_clean_pass_rate ✅, guardrails.directive_detection_rate ✅, guardrails.directive_clean_pass_rate ✅, guardrails.injection_contained ✅.

## Honest caveats

- Synthetic personas: real bank data is messier. Treat these numbers as regression signals, not as promises about every bank's descriptors.
- LLM sampling variance: three fresh full runs of the same commit gave 87.4–89.2% accuracy. Reruns of the same prompts are served from the L1 cache and are exactly reproducible.
- Unresolved transactions count as wrong for accuracy; coverage and precision are reported separately.
- Anomaly and recurring suites use ground-truth categories (they measure the detectors, not the categorizer). Anomalies below the per-currency floor (e.g. a 5× spike on a ₹60 chai) are out of scope by design; recall on all injected anomalies is reported too. Natural large purchases in the synthetic data (a first flight, a big Amazon order) are not labelled anomalies, so flagging them counts against precision.
- Anomaly `min_ratio` was raised from 2.0 to 3.0 after inspecting the first run's false positives on these same personas (see DESIGN_NOTES.md). Held-out personas (same definitions, new seeds, never inspected) are reported separately and held to the same gates.
- Memory: facts and questions were written once, before the first run. One router rule was added after the first full run showed "Which category grew the most last month?" answered with top categories (grounded but off-topic); see DESIGN_NOTES §11.
- FX & MCP: building this suite found a real rate-lookup bug (an earlier stored rate was reused for an unfetched business day; fixed with a regression test) and led to the chat tool reporting rates as amounts so quoted rates can be grounded; see DESIGN_NOTES §13.
- Answer cache: the router's rule patterns and the eval phrasings were written together, so rules-only router accuracy is optimistic; the remaining misroutes were not tuned away. The offline hit rate is low by design (without Mesh, LLM-phrased answers are degraded and never cached). Latencies are in-process.
