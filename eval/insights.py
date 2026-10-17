"""Deterministic eval insights (rules first; an optional grounded LLM narrative comes later)."""

from __future__ import annotations

from typing import Any

from eval.suites.base import SuiteResult

REGRESSION_TOLERANCE = 0.02
# metrics where a LOWER value is better (errors, false alarms)
LOWER_IS_BETTER = (
    "fp_per_month_clean",
    "amount_mape",
    "next_date_mae_days",
    "nmae_30",
    "nmae_60",
    "nmae_90",
    "cached_p95_ms",
    "miss_p95_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "degraded_share",
    "llm_calls_session",
    "chat_fallback_rate",
    "fallback_rate",
    "scope_rules_false_refusal",
    "scope_false_refusal",
    "pii_false_mask_rate",
    "pii_mask_p95_ms",
    "heldout_fp_per_month_clean",
)
# noisy wall-clock / cost metrics: gated, but not compared run-to-run
SKIP = (
    "n_cases",
    "n_truth",
    "n_truth_material",
    "n_property_checks",
    "miss_p95_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "cost_saved_usd",
    "pii_mask_p95_ms",
    "llm_calls_session",
    "chat_p50_ms",
    "chat_p95_ms",
)


def generate(
    results: list[SuiteResult],
    gates: list[dict[str, Any]],
    current: dict[str, float],
    previous: dict[str, float] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in gates:
        if not g["passed"]:
            out.append(
                {
                    "severity": "critical",
                    "title": f"Gate failed: {g['metric']}",
                    "body": f"{g['metric']} = {g['value']:.3f} violates {g['rule']}.",
                    "evidence": g,
                }
            )
    if previous:
        for k, v in current.items():
            name = k.split(".")[-1]
            if k not in previous or name in SKIP:
                continue
            tol = REGRESSION_TOLERANCE * (
                max(abs(previous[k]), 1.0) if name in LOWER_IS_BETTER else 1.0
            )
            worse = v > previous[k] + tol if name in LOWER_IS_BETTER else v < previous[k] - tol
            better = v < previous[k] - tol if name in LOWER_IS_BETTER else v > previous[k] + tol
            if worse:
                out.append(
                    {
                        "severity": "warning",
                        "title": f"Regression: {k}",
                        "body": f"{k} went from {previous[k]:.3f} to {v:.3f} versus the previous comparable run.",
                        "evidence": {"previous": previous[k], "current": v},
                    }
                )
            elif better:
                out.append(
                    {
                        "severity": "info",
                        "title": f"Improved: {k}",
                        "body": f"{k} went from {previous[k]:.3f} to {v:.3f}.",
                        "evidence": {"previous": previous[k], "current": v},
                    }
                )
    for r in results:
        if r.suite != "categorization":
            continue
        weak = [
            c for c in r.details.get("per_category", []) if c["f1"] < 0.6 and c["support"] >= 10
        ]
        for c in weak[:5]:
            conf = [x for x in r.details.get("confusions", []) if x["true"] == c["category"]][:3]
            out.append(
                {
                    "severity": "warning",
                    "title": f"Weak category: {c['category']}",
                    "body": (
                        f"F1 {c['f1']:.2f} (precision {c['precision']:.2f}, recall {c['recall']:.2f}, "
                        f"support {c['support']}). Most often confused with: "
                        + (", ".join(f"{x['pred']} ×{x['count']}" for x in conf) or "n/a")
                        + "."
                    ),
                    "evidence": {"category": c, "confusions": conf},
                }
            )
        for b in r.details.get("calibration", []):
            gap = b["mean_confidence"] - b["accuracy"]
            if b["n"] >= 20 and abs(gap) > 0.15:
                kind = "over" if gap > 0 else "under"
                out.append(
                    {
                        "severity": "warning",
                        "title": f"Calibration: {kind}confident in {b['lo']:.2f}–{b['hi']:.2f}",
                        "body": (
                            f"Mean confidence {b['mean_confidence']:.2f} vs accuracy {b['accuracy']:.2f} "
                            f"over {b['n']} predictions."
                        ),
                        "evidence": b,
                    }
                )
        m = {
            (x.metric, x.dimension.get("variant")): x.value
            for x in r.metrics
            if x.dimension.get("scope") == "overall"
        }
        primary = r.details.get("primary_variant")
        acc, unseen = m.get(("accuracy", primary)), m.get(("unseen_merchant_accuracy", primary))
        if primary == "full" and acc is not None and unseen is not None and acc - unseen > 0.15:
            out.append(
                {
                    "severity": "info",
                    "title": "Unseen merchants are much harder",
                    "body": f"Accuracy {unseen:.2f} on merchants unknown to rules and memory vs {acc:.2f} overall.",
                    "evidence": {"accuracy": acc, "unseen": unseen},
                }
            )
        per_persona = [
            (x.dimension["persona"], x.value)
            for x in r.metrics
            if x.metric == "accuracy"
            and x.dimension.get("scope") == "persona"
            and x.dimension.get("variant") == primary
        ]
        if per_persona:
            worst = min(per_persona, key=lambda kv: kv[1])
            mean = sum(v for _, v in per_persona) / len(per_persona)
            if mean - worst[1] > 0.08:
                out.append(
                    {
                        "severity": "info",
                        "title": f"Weakest persona: {worst[0]}",
                        "body": f"Accuracy {worst[1]:.2f} vs {mean:.2f} average across personas.",
                        "evidence": dict(per_persona),
                    }
                )
        cost = next((x.value for x in r.metrics if x.metric == "llm_cold_cost_usd_per_1k"), None)
        if cost is not None:
            out.append(
                {
                    "severity": "info",
                    "title": "LLM cost",
                    "body": f"${cost:.4f} per 1,000 transactions categorized (Mesh, after rules and memory).",
                    "evidence": {"usd_per_1k": cost},
                }
            )
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda i: order[i["severity"]])
