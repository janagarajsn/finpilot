"""Anomaly-detection suite (deterministic: identical in offline and full mode).

Per persona: load the history months as a fresh synthetic user with ground-truth categories,
run the Analyzer over the WHOLE history (as_of = last history day, no lookback) and match its
transaction-level findings to the injected anomalies:

  truth duplicate      ↔ duplicate_charge
  truth spike          ↔ merchant_outlier | category_outlier
  truth large_one_off  ↔ first_time_large | merchant_outlier | category_outlier
  truth price_hike     ↔ price_hike

A finding matches when its evidence shares a transaction with the truth anomaly. Truth anomalies
below the product's materiality floor (`min_anomaly_amount`, or the small-purchase ceiling for
duplicates; currencies.yml) are out of scope by design: recall is reported on material anomalies
(primary) and on all of them. False positives per month are measured on CLEAN variants of every
persona (same seed, nothing injected), so every transaction-level finding there is a false alarm.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from eval import loader
from eval.metrics import prf
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import Dataset, generate
from eval.synth.personas import PERSONAS, Persona
from finpilot.agents.analyzer import Analyzer
from finpilot.db.session import user_session
from finpilot.insights.models import TXN_ANOMALY_TYPES, Finding
from finpilot.money import get_currency_registry

log = logging.getLogger(__name__)
SUITE = "anomalies"
MATCH = {
    "duplicate": {"duplicate_charge"},
    "spike": {"merchant_outlier", "category_outlier"},
    "large_one_off": {"first_time_large", "merchant_outlier", "category_outlier"},
    "price_hike": {"price_hike"},
}
FINDING_GROUP = {
    "duplicate_charge": "duplicate",
    "merchant_outlier": "spike",
    "category_outlier": "spike",
    "first_time_large": "large_one_off",
    "price_hike": "price_hike",
}


def clean(p: Persona) -> Persona:
    """Same persona and seed with nothing injected."""
    return dataclasses.replace(
        p,
        name=f"{p.name}__clean",
        duplicates=0,
        spikes=0,
        one_off=None,
        series=[dataclasses.replace(s, hike=None) for s in p.series],
    )


def _analyze(ds: Dataset, tag: str) -> tuple[loader.Loaded, list[Finding], dict[str, Decimal]]:
    ld = loader.load(ds, tag)
    loader.apply_truth_categories(ds, ld)
    as_of = max(t.date for t in ds.history())
    with user_session(ld.user_id) as s:
        rep = Analyzer().run(s, ld.user_id, as_of=as_of, lookback_days=None)
        amounts = {
            str(r.id): abs(Decimal(r.amount_reporting))
            for r in s.execute(
                text(
                    "SELECT id, amount_reporting FROM v_transactions_reporting WHERE user_id = :u"
                ),
                {"u": ld.user_id},
            )
        }
    return ld, [f for f in rep.findings if f.type in TXN_ANOMALY_TYPES], amounts


def _material(a: Any, ds: Dataset, ld: loader.Loaded, amounts: dict[str, Decimal]) -> bool:
    thr = get_currency_registry().get(ds.persona.reporting_currency).thresholds
    if a.type == "price_hike":
        return True
    amt = max(amounts.get(str(ld.txn_ids[i]), Decimal(0)) for i in a.txn_ids if i in ld.txn_ids)
    return (
        amt > thr.small_purchase_ceiling if a.type == "duplicate" else amt >= thr.min_anomaly_amount
    )


HELDOUT_SEED_OFFSET = 7919  # held-out personas: same definitions, new seeds, never inspected


def heldout(p: Persona) -> Persona:
    return dataclasses.replace(p, name=f"{p.name}__heldout", seed=p.seed + HELDOUT_SEED_OFFSET)


def _counts(ds: Dataset, tag: str) -> dict[str, float]:
    """Material recall / finding precision counts for one dataset (same matching as `run`)."""
    ld, found, amounts = _analyze(ds, tag)
    matched: set[str] = set()
    c: dict[str, float] = {"truth_material": 0, "tp_material": 0}
    for a in ds.anomalies:
        ids = {str(ld.txn_ids[i]) for i in a.txn_ids if i in ld.txn_ids}
        if not ids:
            continue
        hits = [f for f in found if f.type in MATCH[a.type] and ids & set(f.evidence_txn_ids)]
        matched |= {f.fingerprint for f in hits}
        if _material(a, ds, ld, amounts):
            c["truth_material"] += 1
            c["tp_material"] += bool(hits)
    loader.cleanup(tag)
    return c | {"findings": len(found), "tp_findings": sum(f.fingerprint in matched for f in found)}


def _heldout_metrics(personas: list[Persona], tag: str) -> dict[str, Any]:
    tot: dict[str, float] = defaultdict(float)
    for p in personas:
        h = heldout(p)
        for k, v in _counts(generate(h), tag).items():
            tot[k] += v
        cds = generate(clean(h))
        _, cfound, _ = _analyze(cds, tag)
        tot["clean_fp"] += len(cfound)
        tot["clean_months"] += (max(t.date for t in cds.history()) - cds.persona.start).days / 30.44
        loader.cleanup(tag)
    m = prf(
        int(tot["tp_findings"]),
        int(tot["findings"] - tot["tp_findings"]),
        int(tot["truth_material"] - tot["tp_material"]),
    )
    return {
        "precision": m["precision"],
        "recall": m["recall"],
        "fp_per_month_clean": tot["clean_fp"] / max(tot["clean_months"], 1e-9),
        "totals": {k: round(v, 2) for k, v in tot.items()},
    }


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas]
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tot: dict[str, int] = defaultdict(int)
    clean_fp, clean_months = 0, 0.0
    per_persona: list[dict[str, Any]] = []
    for p in personas:
        ds = generate(p)
        ld, found, amounts = _analyze(ds, ctx.run_tag)
        db_to_syn = {str(v): k for k, v in ld.txn_ids.items()}
        matched_findings: set[str] = set()
        pp: dict[str, int] = defaultdict(int)
        for a in ds.anomalies:
            ids = {str(ld.txn_ids[i]) for i in a.txn_ids if i in ld.txn_ids}
            if not ids:
                continue  # anomaly in held-out months
            hits = [f for f in found if f.type in MATCH[a.type] and ids & set(f.evidence_txn_ids)]
            material = _material(a, ds, ld, amounts)
            detected = bool(hits)
            matched_findings |= {f.fingerprint for f in hits}
            by_type[a.type]["truth"] += 1
            by_type[a.type]["tp_all"] += detected
            tot["truth"] += 1
            tot["tp_all"] += detected
            if material:
                by_type[a.type]["truth_material"] += 1
                by_type[a.type]["tp_material"] += detected
                tot["truth_material"] += 1
                tot["tp_material"] += detected
                pp["truth_material"] += 1
                pp["tp_material"] += detected
            res.cases.append(
                Case(
                    case_id=a.id,
                    input={
                        "persona": p.name,
                        "type": a.type,
                        "date": a.date.isoformat(),
                        "detail": a.detail,
                        "txn_ids": a.txn_ids,
                        "material": material,
                    },
                    expected={"detected": True, "finding_types": sorted(MATCH[a.type])},
                    actual={
                        "detected": detected,
                        "findings": [{"type": f.type, "title": f.title} for f in hits],
                    },
                    passed=detected or not material,
                    score=1.0 if detected else 0.0,
                    error_type=None if detected else ("missed" if material else "below_floor"),
                )
            )
        for f in found:
            group = FINDING_GROUP[f.type]
            by_type[group]["findings"] += 1
            tot["findings"] += 1
            ok = f.fingerprint in matched_findings
            by_type[group]["tp_findings"] += ok
            tot["tp_findings"] += ok
            pp["findings"] += 1
            pp["tp_findings"] += ok
            if not ok:
                res.cases.append(
                    Case(
                        case_id=f"{p.name}:fp:{f.fingerprint}",
                        input={
                            "persona": p.name,
                            "finding_type": f.type,
                            "evidence": [db_to_syn.get(e, e) for e in f.evidence_txn_ids],
                        },
                        expected={"anomaly": False},
                        actual={"title": f.title, "body": f.body, "severity": f.severity},
                        passed=False,
                        score=0.0,
                        error_type="false_positive",
                    )
                )
        loader.cleanup(ctx.run_tag)
        # clean variant: every transaction-level finding is a false alarm
        cds = generate(clean(p))
        _, cfound, _ = _analyze(cds, ctx.run_tag)
        months = (max(t.date for t in cds.history()) - cds.persona.start).days / 30.44
        clean_fp += len(cfound)
        clean_months += months
        loader.cleanup(ctx.run_tag)
        per_persona.append(
            {
                "persona": p.name,
                "currency": p.reporting_currency,
                **prf(
                    pp["tp_findings"],
                    pp["findings"] - pp["tp_findings"],
                    pp["truth_material"] - pp["tp_material"],
                ),
                "clean_fp": len(cfound),
                "clean_fp_per_month": round(len(cfound) / months, 3),
                "clean_fp_types": sorted({f.type for f in cfound}),
            }
        )

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    overall = prf(
        tot["tp_findings"],
        tot["findings"] - tot["tp_findings"],
        tot["truth_material"] - tot["tp_material"],
    )
    add("precision", overall["precision"], "ratio", scope="overall", primary=True)
    add("recall", overall["recall"], "ratio", scope="overall", primary=True, basis="material")
    add("f1", overall["f1"], "ratio", scope="overall", primary=True)
    add("recall_all", tot["tp_all"] / max(tot["truth"], 1), "ratio", scope="overall", primary=True)
    add(
        "fp_per_month_clean",
        clean_fp / max(clean_months, 1e-9),
        "count",
        scope="overall",
        primary=True,
    )
    add("n_truth", tot["truth"], "count", scope="overall", primary=True)
    add("n_truth_material", tot["truth_material"], "count", scope="overall", primary=True)
    table = []
    for t in sorted(MATCH):
        b = by_type[t]
        m = prf(
            b["tp_findings"],
            b["findings"] - b["tp_findings"],
            b["truth_material"] - b["tp_material"],
        )
        for k in ("precision", "recall", "f1"):
            add(k, m[k], "ratio", scope="type", type=t)
        table.append(
            {
                "type": t,
                **{k: round(v, 4) for k, v in m.items()},
                "truth": b["truth"],
                "truth_material": b["truth_material"],
                "detected_material": b["tp_material"],
                "findings": b["findings"],
                "true_findings": b["tp_findings"],
            }
        )
    for row in per_persona:
        add("f1", row["f1"], "ratio", scope="persona", persona=row["persona"])
        add(
            "fp_per_month_clean",
            row["clean_fp_per_month"],
            "count",
            scope="persona",
            persona=row["persona"],
        )
    ho = _heldout_metrics(personas, ctx.run_tag)
    add("heldout_precision", ho["precision"], "ratio", scope="overall", primary=True)
    add("heldout_recall", ho["recall"], "ratio", scope="overall", primary=True, basis="material")
    add(
        "heldout_fp_per_month_clean",
        ho["fp_per_month_clean"],
        "count",
        scope="overall",
        primary=True,
    )
    res.details = {
        "heldout": ho,
        "per_type": table,
        "per_persona": per_persona,
        "totals": dict(tot),
        "clean": {"false_positives": clean_fp, "months": round(clean_months, 1)},
    }
    return res
