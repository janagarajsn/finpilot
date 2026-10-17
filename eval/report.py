"""Writes eval/results/RESULTS.md from the latest offline and full runs stored in Postgres."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from eval.store import engine

OUT = Path(__file__).resolve().parent / "results" / "RESULTS.md"


def _latest(mode: str) -> dict[str, Any] | None:
    with engine().connect() as c:
        row = (
            c.execute(
                text("""SELECT * FROM eval_runs WHERE mode = :m AND status IN ('passed','failed')
                                ORDER BY started_at DESC LIMIT 1"""),
                {"m": mode},
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        run = dict(row)
        run["metrics"] = [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT suite, metric, value, unit, dimension_json AS d FROM eval_metrics WHERE run_id = :r"
                ),
                {"r": run["id"]},
            ).mappings()
        ]
        return run


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render() -> str:
    lines = [
        "# FinPilot evaluation results",
        "",
        "Generated from Postgres (`eval_runs` / `eval_metrics`) by `python -m eval.report`. "
        "Do not edit by hand. Personas are seeded synthetic INR/USD users with ground truth "
        "(see DESIGN_NOTES.md § Evaluation).",
        "",
    ]
    for mode in ("full", "offline"):
        run = _latest(mode)
        if not run:
            lines += [f"## {mode.title()} run", "", "_Not run yet._", ""]
            continue
        lines += [
            f"## {mode.title()} run ({'rules + memory + LLM via Mesh' if mode == 'full' else 'rules + memory, no LLM'})",
            "",
            f"- Run `{run['id']}` · {run['started_at']:%Y-%m-%d %H:%M} UTC · commit `{run['git_sha']}` · "
            f"status **{run['status']}**",
            f"- Model: `{run['models_json'].get('fast')}` · prompt `{run['prompt_versions_json'].get('categorizer/classify')}`",
            "",
        ]
        details = (run["summary_json"].get("details") or {}).get("categorization", {})
        lines += [
            "### Categorization: layer ablation",
            "",
            "| Variant | Accuracy | Coverage | Precision when answering | Macro-F1 |",
            "|---|---:|---:|---:|---:|",
        ]
        for v, label in (
            ("rules", "Rules only"),
            ("rules_memory", "+ merchant memory"),
            ("full", "+ LLM (Mesh)"),
        ):
            m = details.get("variants", {}).get(v)
            if m:
                lines.append(
                    f"| {label} | {_pct(m['accuracy'])} | {_pct(m['coverage'])} | "
                    f"{_pct(m['precision_resolved'])} | {m['macro_f1']:.3f} |"
                )
        h = run["summary_json"].get("headline", {})
        lines += ["", "| Metric (primary variant) | Value |", "|---|---:|"]
        for k, label in (
            ("unseen_merchant_accuracy", "Unseen-merchant accuracy"),
            ("ambiguous_accuracy", "Ambiguous-merchant accuracy"),
            ("custom_category_recall", "Custom-category recall"),
            ("injection_safe", "Prompt-injection safe"),
            ("n_cases", "Scored transactions"),
        ):
            v = h.get(f"categorization.{k}")
            if v is not None:
                lines.append(f"| {label} | {int(v) if k == 'n_cases' else _pct(v)} |")
        for m in run["metrics"]:
            if m["d"].get("scope") == "cost" and m["metric"] in (
                "llm_cold_cost_usd_per_1k",
                "llm_cold_calls_per_1k",
                "llm_cost_usd_per_1k",
                "cached_token_share",
            ):
                val = (
                    f"${m['value']:.4f}"
                    if "usd" in m["metric"]
                    else _pct(m["value"])
                    if m["metric"] == "cached_token_share"
                    else f"{m['value']:.2f}"
                )
                label = {
                    "llm_cold_cost_usd_per_1k": "LLM cost per 1k transactions (uncached run)",
                    "llm_cold_calls_per_1k": "LLM calls per 1k transactions",
                    "llm_cost_usd_per_1k": "LLM spend per 1k this run (after L1 cache)",
                    "cached_token_share": "Prompt tokens served from provider cache",
                }[m["metric"]]
                lines.append(f"| {label} | {val} |")
        per_persona = sorted(
            (m["d"]["persona"], m["value"])
            for m in run["metrics"]
            if m["metric"] == "accuracy"
            and m["d"].get("scope") == "persona"
            and m["d"].get("primary")
        )
        if per_persona:
            lines += ["", "| Persona | Accuracy |", "|---|---:|"] + [
                f"| {p} | {_pct(v)} |" for p, v in per_persona
            ]
        weak = [c for c in details.get("per_category", []) if c["f1"] < 0.7][:6]
        if weak:
            lines += [
                "",
                "Weakest categories (F1 < 0.70): "
                + ", ".join(f"{c['category']} ({c['f1']:.2f}, n={int(c['support'])})" for c in weak)
                + ".",
            ]
        all_details = run["summary_json"].get("details") or {}
        ana = all_details.get("anomalies")
        if ana:
            lines += [
                "",
                "### Anomaly detection (deterministic, injected ground truth)",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Precision | {_pct(h.get('anomalies.precision'))} |",
                f"| Recall on material anomalies | {_pct(h.get('anomalies.recall'))} |",
                f"| Recall on all injected anomalies | {_pct(h.get('anomalies.recall_all'))} |",
                f"| F1 | {h.get('anomalies.f1', 0):.3f} |",
                f"| False alarms per month on clean personas | {h.get('anomalies.fp_per_month_clean', 0):.2f} |",
                f"| Held-out personas (new seeds): precision / recall / false alarms per clean month | "
                f"{_pct(h.get('anomalies.heldout_precision'))} / {_pct(h.get('anomalies.heldout_recall'))} / "
                f"{h.get('anomalies.heldout_fp_per_month_clean', 0):.2f} |",
                "",
                "| Type | Injected (material) | Found | Precision | Recall |",
                "|---|---:|---:|---:|---:|",
            ] + [
                f"| {t['type'].replace('_', ' ')} | {t['truth']} ({t['truth_material']}) | "
                f"{t['detected_material']} | {_pct(t['precision'])} | {_pct(t['recall'])} |"
                for t in ana.get("per_type", [])
            ]
        rec = all_details.get("recurring")
        if rec:
            lines += [
                "",
                "### Recurring detection and materializer",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Precision | {_pct(h.get('recurring.precision'))} |",
                f"| Recall | {_pct(h.get('recurring.recall'))} |",
                f"| Period accuracy | {_pct(h.get('recurring.period_accuracy'))} |",
                f"| Amount error (MAPE vs nominal price) | {_pct(h.get('recurring.amount_mape'))} |",
                f"| Next-date error (mean days) | {h.get('recurring.next_date_mae_days', 0):.2f} |",
                f"| Next date within ±3 days | {_pct(h.get('recurring.next_date_within_3d'))} |",
                f"| Materializer correct (month-end, biweekly, quarterly, catch-up) | {_pct(h.get('recurring.materializer_correct'))} |",
                f"| Materializer idempotent (repeat ticks create nothing) | {_pct(h.get('recurring.materializer_idempotent'))} |",
            ]
        fc = all_details.get("forecast")
        if fc:
            lines += (
                [
                    "",
                    f"### Forecast backtest ({fc.get('origins')} rolling origins × 90 days, ground-truth categories)",
                    "",
                    "| Horizon | Error: model | Trailing-mean baseline | Last-month baseline | Actual inside p10–p90 |",
                    "|---|---:|---:|---:|---:|",
                ]
                + [
                    f"| {r['horizon']} days | {r['model']:.2f} | {r['trailing_mean']:.2f} | {r['last_month']:.2f} | {_pct(r['coverage'])} |"
                    for r in fc.get("per_horizon", [])
                ]
                + [
                    "",
                    "Error = |median forecast − actual balance| ÷ the persona's average monthly spend. "
                    f"Days inside the p10–p90 band: {_pct(h.get('forecast.coverage_daily'))} (target ≈ 80%). "
                    f"Skill vs trailing mean {_pct(h.get('forecast.skill_vs_trailing_mean'))}, vs last month "
                    f"{_pct(h.get('forecast.skill_vs_last_month'))} (share of error removed). Sparse-data handling: "
                    f"{'ok' if h.get('forecast.sparse_ok') == 1 else 'FAILED'}.",
                ]
            )
        sim = all_details.get("simulation")
        if sim:
            props = sim.get("properties", {})
            lines += [
                "",
                "### What-if simulator",
                "",
                f"- Property checks: {sum(v['passed'] for v in props.values())}/{sum(v['checks'] for v in props.values())} held "
                f"({', '.join(k.replace('_', ' ') for k in props)}).",
            ] + [
                f"- Real one-off purchase ({r['persona']}, {r['date']}): actual balance inside the band on "
                f"{_pct(r['coverage_scenario'])} of days with the what-if vs {_pct(r['coverage_baseline'])} without it."
                for r in sim.get("historical", [])
            ]
            nl = sim.get("nl") or {}
            if nl:
                lines.append(
                    f"- Natural-language parsing (18 questions, Mesh): intent {_pct(nl['nl_intent_accuracy'])}, slots "
                    f"{_pct(nl['nl_slot_accuracy'])}, asks for missing amounts {_pct(nl['nl_clarification_accuracy'])}, fully right "
                    f"{_pct(nl['nl_exact'])}. Prompt v2 was revised after v1 missed 2 of these questions, so this is optimistic."
                )
        adv = all_details.get("advisor")
        if adv:
            lines += [
                "",
                f"### Advisor ({'smart LLM via Mesh + grounding guard' if mode == 'full' else 'deterministic templates'})",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Recommendations cite ≥ 1 finding | {_pct(h.get('advisor.pct_with_finding'))} |",
                f"| Name a specific merchant / category / goal | {_pct(h.get('advisor.pct_specific'))} |",
                f"| Every number grounded in computed facts | {_pct(h.get('advisor.pct_grounded'))} |",
                f"| Personas with a top recommendation of their own | {_pct(h.get('advisor.personas_distinct_top'))} |",
                f"| Dismissed advice not repeated | {_pct(h.get('advisor.dismissed_not_repeated'))} |",
                f"| Guard catches corrupted numbers (scaled, fabricated, wrong currency) | {_pct(h.get('advisor.guard_detection_rate'))} |",
            ]
            if h.get("advisor.grounding_first_attempt_pass_rate") is not None:
                lines += [
                    f"| Grounding passes on the first attempt | {_pct(h.get('advisor.grounding_first_attempt_pass_rate'))} |",
                    f"| Fell back to templates after 2 retries | {_pct(h.get('advisor.fallback_rate'))} |",
                    f"| LLM-as-judge overall (rubric prompts/eval_judge/advice.v1.md, 1–5 scaled to 0–1) | {_pct(h.get('advisor.judge_overall'))} |",
                ]
        cache = all_details.get("cache")
        if cache:
            lat = cache.get("latency", {})
            names = {
                "L2": "L2 exact answer",
                "L3": "L3 semantic",
                "miss": "computed (miss)",
                "none": "not cacheable",
            }
            rt = cache.get("router", {})
            lines += [
                "",
                f"### Answer cache ({cache['questions']}-question session, writes at {' and '.join(map(str, cache['writes_at']))})",
                "",
                f"- Hit rate on cacheable answers {_pct(h.get('cache.hit_rate'))}; invalidated after writes "
                f"{_pct(h.get('cache.invalidation_correct'))} ({cache['invalidation_checks']} checks); cross-user isolation "
                f"{_pct(h.get('cache.cross_user_isolation'))} ({cache['isolation_checks']} checks); degraded answers "
                f"{_pct(h.get('cache.degraded_share'))}.",
                f"- Router: {rt.get('correct')}/{rt.get('total')} labelled phrasings "
                f"({', '.join(f'{k} {v}' for k, v in rt.get('by_layer', {}).items())}).",
            ]
            if h.get("cache.cost_saved_usd") is not None:
                lines.append(
                    f"- LLM cost saved by hits in the session: ${h['cache.cost_saved_usd']:.4f}."
                )
            lines += ["", "| Layer | Answers | p50 ms | p95 ms |", "|---|---:|---:|---:|"] + [
                f"| {names[k]} | {lat[k]['n']} | {lat[k]['p50']:.0f} | {lat[k]['p95']:.0f} |"
                for k in names
                if k in lat
            ]
        mem = all_details.get("memory")
        if mem:
            lines += [
                "",
                f"### Memory ({'Mesh embeddings' if mem.get('retrieval') == 'vector' else 'lexical retrieval, no Mesh'})",
                "",
                f"- Recall on {mem['questions']} questions over {mem['facts']} remembered facts: hit@1 "
                f"{_pct(h.get('memory.hit_at_1'))}, hit@{mem['k']} {_pct(h.get('memory.hit_at_' + str(mem['k'])))}.",
                f"- A newer fact replaces a contradicted one {_pct(h.get('memory.supersede_correct'))}; deleted memories "
                f"never recalled again {_pct(h.get('memory.deletion_removes_recall'))}; account numbers / credentials "
                f"rejected {_pct(h.get('memory.blocked_rejected'))}; other users recall nothing "
                f"{_pct(h.get('memory.cross_user_isolation'))}.",
            ]
            if h.get("memory.writer_recall") is not None:
                w = mem.get("writer", {})
                lines.append(
                    f"- Memory writer on {w.get('chats')} synthetic chats: found {_pct(h.get('memory.writer_recall'))} "
                    f"of the expected memories, {_pct(h.get('memory.writer_precision'))} of what it stored was expected, "
                    f"stored no quoted figures or account numbers in {_pct(h.get('memory.writer_no_leak'))} of chats."
                )
            if h.get("memory.chat_grounded") is not None:
                routes = ", ".join(
                    f"{k} {v}" for k, v in (mem.get("chat") or {}).get("routes", {}).items()
                )
                lines.append(
                    f"- Chat on the sample data ({(mem.get('chat') or {}).get('questions')} questions; routes: {routes}): "
                    f"every number verified in {_pct(h.get('memory.chat_grounded'))} of answers, fallback "
                    f"{_pct(h.get('memory.chat_fallback_rate'))}, p50 {h.get('memory.chat_p50_ms', 0) / 1000:.1f} s, "
                    f"p95 {h.get('memory.chat_p95_ms', 0) / 1000:.1f} s. Grounded is not the same as relevant: answers "
                    "are checked by hand in the failures explorer."
                )
        fxd = all_details.get("fx")
        if fxd is not None and h.get("fx.conversion_accuracy") is not None:
            lines += [
                "",
                "### FX & MCP (no network: in-process MCP server, fake HTTP)",
                "",
                "| Check | Result |",
                "|---|---:|",
            ] + [
                f"| {label} | {_pct(h.get('fx.' + k))} |"
                for k, label in (
                    ("conversion_accuracy", "Conversions exact vs reference table"),
                    ("round_trip_within_0_01", "USD → INR → USD within $0.01"),
                    ("date_resolution", "Weekend / holiday / future dates"),
                    (
                        "fallback_chain",
                        "Fallback chain: source and flags (MCP down, HTTP down, stale DB, pinned)",
                    ),
                    (
                        "mcp_contract",
                        "MCP contract (tools = allowlist, shapes, tool errors, caching)",
                    ),
                    ("allowlist_enforced", "Allowlist enforced before any I/O"),
                    ("circuit_breaker", "Circuit breaker opens, half-opens, recovers"),
                    (
                        "grounding_converted_detection",
                        "Wrong converted amounts caught by the guard",
                    ),
                    ("grounding_converted_clean_pass", "Correct converted amounts pass the guard"),
                    ("fx_exposure_correct", "fx_exposure vs generator ground truth"),
                )
                if h.get("fx." + k) is not None
            ]
        gd = all_details.get("guardrails")
        if gd is not None and h.get("guardrails.pii_mask_recall") is not None:
            lines += (
                [
                    "",
                    "### Guardrails (deterministic)",
                    "",
                    "| Check | Result |",
                    "|---|---:|",
                ]
                + [
                    f"| {label} | {_pct(h.get('guardrails.' + k))} |"
                    for k, label in (
                        ("pii_mask_recall", "Identifiers masked before Mesh / Langfuse"),
                        ("pii_false_mask_rate", "Amounts, dates, references wrongly masked"),
                        ("leak_detection_rate", "Answers with an identifier blocked"),
                        ("leak_clean_pass_rate", "Clean answers passed"),
                        ("directive_detection_rate", "Investment / tax / legal directives flagged"),
                        ("directive_clean_pass_rate", "FinPilot levers passed"),
                        ("injection_contained", "Injected text kept inside <data> blocks"),
                    )
                    if h.get("guardrails." + k) is not None
                ]
                + [f"| Masking p95 | {h.get('guardrails.pii_mask_p95_ms', 0):.1f} ms |"]
            )
        gates = run["summary_json"].get("gates", [])
        if gates:
            lines += [
                "",
                "Gates: "
                + ", ".join(f"{g['metric']} {'✅' if g['passed'] else '❌'}" for g in gates)
                + ".",
            ]
        lines.append("")
    lines += [
        "## Honest caveats",
        "",
        "- Synthetic personas: real bank data is messier. Treat these numbers as regression signals, not as "
        "promises about every bank's descriptors.",
        "- LLM sampling variance: three fresh full runs of the same commit gave 87.4–89.2% accuracy. Reruns of "
        "the same prompts are served from the L1 cache and are exactly reproducible.",
        "- Unresolved transactions count as wrong for accuracy; coverage and precision are reported separately.",
        "- Anomaly and recurring suites use ground-truth categories (they measure the detectors, not the "
        "categorizer). Anomalies below the per-currency floor (e.g. a 5× spike on a ₹60 chai) are out of scope by "
        "design; recall on all injected anomalies is reported too. Natural large purchases in the synthetic data "
        "(a first flight, a big Amazon order) are not labelled anomalies, so flagging them counts against precision.",
        "- Anomaly `min_ratio` was raised from 2.0 to 3.0 after inspecting the first run's false positives on these "
        "same personas (see DESIGN_NOTES.md). Held-out personas (same definitions, new seeds, never inspected) "
        "are reported separately and held to the same gates.",
        "- Memory: facts and questions were written once, before the first run. One router rule was added after the "
        'first full run showed "Which category grew the most last month?" answered with top categories (grounded but '
        "off-topic); see DESIGN_NOTES §11.",
        "- FX & MCP: building this suite found a real rate-lookup bug (an earlier stored rate was reused for "
        "an unfetched business day; fixed with a regression test) and led to the chat tool reporting rates as "
        "amounts so quoted rates can be grounded; see DESIGN_NOTES §13.",
        "- Answer cache: the router's rule patterns and the eval phrasings were written together, so rules-only "
        "router accuracy is optimistic; the remaining misroutes were not tuned away. The offline hit rate is low by "
        "design (without Mesh, LLM-phrased answers are degraded and never cached). Latencies are in-process.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
