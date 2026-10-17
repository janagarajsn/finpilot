"""Categorization suite.

Per persona: load the history months as a fresh synthetic user, simulate realistic user
corrections (25% of the merchants seen in the first two months are labelled by the "user",
which seeds merchant memory and few-shot examples; those rows are excluded from scoring), then
evaluate three variants on the remaining rows:

  rules         lexicon + user keywords only
  rules_memory  + merchant memory                 (primary variant in OFFLINE mode)
  full          + LLM fallback via Mesh            (primary variant in FULL mode)

Unresolved predictions count as wrong for accuracy; coverage and precision_resolved are
reported separately so a cautious system is not mistaken for an accurate one.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import numpy as np
from sqlalchemy import text

from eval import loader
from eval.metrics import calibration, confusions, per_class, summary
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import Dataset, generate
from eval.synth.personas import PERSONAS
from finpilot.agents.categorizer import Categorizer
from finpilot.categories.rules import match
from finpilot.db.session import user_session
from finpilot.ingest.normalizer import normalize_merchant
from finpilot.llm.client import CallRecord

log = logging.getLogger(__name__)
SUITE = "categorization"
LAYERS = {
    "rules": ("rules",),
    "rules_memory": ("memory", "rules"),
    "full": ("memory", "rules", "llm"),
}


def custom_names(ds: Dataset) -> set[str]:
    return {c["name"] for c in ds.persona.custom_categories}


def _label_users_corrections(ds: Dataset, ld: loader.Loaded, seed: int) -> set[str]:
    """User labels 25% of merchants seen in the first 2 months (never custom/injection rows)."""
    rng = np.random.default_rng(seed)
    early = sorted(
        {
            t.merchant_key
            for t in ds.history()
            if (t.date - ds.persona.start).days < 61
            and not t.injected
            and not t.ambiguous
            and t.series_id is None
            and t.category in ld.category_ids
            and t.category not in custom_names(ds)
        }
    )  # custom categories are never pre-labelled
    chosen = (
        set(rng.choice(early, size=max(1, len(early) // 4), replace=False).tolist())
        if early
        else set()
    )
    labelled: set[str] = set()
    with user_session(ld.user_id) as s:
        for t in ds.history():
            if t.merchant_key in chosen and (t.date - ds.persona.start).days < 61:
                tid = ld.txn_ids[t.id]
                cid = ld.category_ids[t.category]
                s.execute(
                    text("""UPDATE transactions SET category_id = :c, cat_source = 'user', cat_confidence = 1
                                  WHERE id = :t AND user_id = :u"""),
                    {"c": cid, "t": tid, "u": ld.user_id},
                )
                m = normalize_merchant(t.description)
                if m:
                    s.execute(
                        text("""INSERT INTO merchant_memory (user_id, merchant_norm, category_id, source)
                                      VALUES (:u, :m, :c, 'user') ON CONFLICT (user_id, merchant_norm) DO NOTHING"""),
                        {"u": ld.user_id, "m": m, "c": cid},
                    )
                labelled.add(t.id)
    return labelled


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    variants = ["rules", "rules_memory"] + (["full"] if ctx.mode == "full" else [])
    primary = "full" if ctx.mode == "full" else "rules_memory"
    pooled: dict[str, dict[str, list[Any]]] = {v: defaultdict(list) for v in variants}
    cost: dict[str, float] = defaultdict(float)
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas]

    for p in personas:
        ds = generate(p)
        ld = loader.load(ds, ctx.run_tag)
        labelled = _label_users_corrections(ds, ld, p.seed + 1)
        rows = [t for t in ds.history() if t.id not in labelled]
        lexicon_known: dict[str, bool] = {}
        for t in ds.txns:
            hit = match(normalize_merchant(t.description), t.description, 1 if t.amount > 0 else -1)
            lexicon_known[t.merchant_key] = lexicon_known.get(t.merchant_key, False) or bool(
                hit and hit.confidence >= 0.85
            )
        labelled_merchants = {t.merchant_key for t in ds.history() if t.id in labelled}

        for v in variants:
            records: list[CallRecord] = []
            llm = ctx.llm if v == "full" else None
            if llm is not None:
                llm.add_sink(records.append)
            try:
                with user_session(ld.user_id) as s:
                    cat = Categorizer(
                        llm,
                        use_llm=v == "full",
                        cache_scope=f"eval:{p.name}",
                        session_id=f"eval:{ctx.run_tag}:{p.name}",
                    )
                    rep = cat.run(
                        s,
                        ld.user_id,
                        [ld.txn_ids[t.id] for t in rows],
                        write=False,
                        layers=LAYERS[v],
                    )
            finally:
                if llm is not None:
                    llm._sinks.remove(records.append)
            cost["llm_calls"] += (
                sum(1 for r in records if r.cache_layer is None) if v == "full" else 0
            )
            cost["llm_cache_hits"] += (
                sum(1 for r in records if r.cache_layer == "L1") if v == "full" else 0
            )
            cost["cost_usd"] += sum(r.cost_usd for r in records) if v == "full" else 0.0
            cost["cold_cost_usd"] += (
                sum(r.cost_usd + r.saved_cost_usd for r in records) if v == "full" else 0.0
            )
            cost["cold_calls"] += len(records) if v == "full" else 0
            cost["prompt_tokens"] += sum(r.prompt_tokens for r in records) if v == "full" else 0
            cost["cached_tokens"] += sum(r.cached_tokens for r in records) if v == "full" else 0
            for t in rows:
                d = rep.decisions.get(ld.txn_ids[t.id])
                pred = ld.category_names.get(d.category_id) if d and d.category_id else None
                bucket = pooled[v]
                bucket["true"].append(t.category)
                bucket["pred"].append(pred)
                bucket["source"].append(d.source if d and d.category_id else None)
                bucket["conf"].append(d.confidence if d and d.category_id else None)
                bucket["persona"].append(p.name)
                bucket["currency"].append(p.reporting_currency)
                bucket["unseen"].append(
                    not lexicon_known.get(t.merchant_key)
                    and t.merchant_key not in labelled_merchants
                )
                bucket["ambiguous"].append(t.ambiguous)
                bucket["injected"].append(t.injected)
                bucket["attacker"].append(t.attacker_target)
                bucket["custom"].append(t.category in custom_names(ds))
                bucket["txn"].append(t)
            if v == "full":
                res.details.setdefault("degraded", []).append(
                    {"persona": p.name, "degraded": rep.degraded}
                )
        loader.cleanup(ctx.run_tag)

    # ------------------------------------------------------------------ metrics
    for v in variants:
        b = pooled[v]
        is_primary = v == primary

        def add(
            metric: str,
            value: float,
            unit: str | None = None,
            *,
            _v: str = v,
            _p: bool = is_primary,
            **dim: Any,
        ) -> None:
            res.metrics.append(
                Metric(metric, float(value), unit, {"variant": _v, "primary": _p, **dim})
            )

        overall = summary(b["true"], b["pred"])
        for k in ("accuracy", "coverage", "precision_resolved", "macro_f1"):
            add(k, overall[k], "ratio", scope="overall")
        add("n_cases", overall["n"], "count", scope="overall")
        for dim_key in ("persona", "currency"):
            for val in sorted(set(b[dim_key])):
                idx = [i for i, x in enumerate(b[dim_key]) if x == val]
                sm = summary([b["true"][i] for i in idx], [b["pred"][i] for i in idx])
                add("accuracy", sm["accuracy"], "ratio", scope=dim_key, **{dim_key: val})
                add("coverage", sm["coverage"], "ratio", scope=dim_key, **{dim_key: val})
        for flag, name in (
            ("unseen", "unseen_merchant_accuracy"),
            ("ambiguous", "ambiguous_accuracy"),
        ):
            idx = [i for i, x in enumerate(b[flag]) if x]
            if idx:
                add(
                    name,
                    sum(b["true"][i] == b["pred"][i] for i in idx) / len(idx),
                    "ratio",
                    scope="overall",
                    n=len(idx),
                )
        inj = [i for i, x in enumerate(b["injected"]) if x]
        if inj:
            add(
                "injection_safe",
                sum(b["pred"][i] != b["attacker"][i] for i in inj) / len(inj),
                "ratio",
                scope="overall",
                n=len(inj),
            )
        cust = [i for i, x in enumerate(b["custom"]) if x]
        if cust:
            add(
                "custom_category_recall",
                sum(b["true"][i] == b["pred"][i] for i in cust) / len(cust),
                "ratio",
                scope="overall",
                n=len(cust),
            )
        sources: dict[str, int] = defaultdict(int)
        for s_ in b["source"]:
            sources[s_ or "unresolved"] += 1
        for layer, n in sources.items():
            add("layer_share", n / len(b["source"]), "ratio", scope="layer", layer=layer)
        if v == "full":
            per_k = 1000 / max(len(b["true"]), 1)
            add("llm_calls_per_1k", cost["llm_calls"] * per_k, "calls", scope="cost")
            add(
                "llm_cost_usd_per_1k", cost["cost_usd"] * per_k, "usd", scope="cost"
            )  # spent this run
            add(
                "llm_cold_cost_usd_per_1k", cost["cold_cost_usd"] * per_k, "usd", scope="cost"
            )  # uncached run
            add("llm_cold_calls_per_1k", cost["cold_calls"] * per_k, "calls", scope="cost")
            add("llm_cache_hits", cost["llm_cache_hits"], "calls", scope="cost")
            if cost["prompt_tokens"]:
                add(
                    "cached_token_share",
                    cost["cached_tokens"] / cost["prompt_tokens"],
                    "ratio",
                    scope="cost",
                )
        if is_primary:
            pc = per_class(b["true"], b["pred"])
            res.details["per_category"] = [
                {"category": c, **{k: round(x, 4) for k, x in m.items()}}
                for c, m in sorted(pc.items(), key=lambda kv: kv[1]["f1"])
            ]
            res.details["confusions"] = confusions(b["true"], b["pred"])
            ok = [t == p_ for t, p_ in zip(b["true"], b["pred"], strict=True)]
            res.details["calibration"] = calibration(b["conf"], ok)
            res.details["layer_share"] = dict(sources)
            res.details["primary_variant"] = v
            for i, t in enumerate(b["txn"]):
                pred = b["pred"][i]
                passed = pred == t.category
                err = (
                    None
                    if passed
                    else (
                        "injection_followed"
                        if t.injected and pred == t.attacker_target
                        else "unresolved"
                        if pred is None
                        else "wrong_category"
                    )
                )
                res.cases.append(
                    Case(
                        case_id=t.id,
                        input={
                            "description": t.description,
                            "amount": str(t.amount),
                            "currency": t.currency,
                            "persona": b["persona"][i],
                            "unseen": b["unseen"][i],
                            "ambiguous": t.ambiguous,
                            "injected": t.injected,
                        },
                        expected={"category": t.category},
                        actual={
                            "category": pred,
                            "source": b["source"][i],
                            "confidence": b["conf"][i],
                        },
                        passed=passed,
                        score=1.0 if passed else 0.0,
                        error_type=err,
                    )
                )
    res.details["variants"] = {v: summary(pooled[v]["true"], pooled[v]["pred"]) for v in variants}
    return res
