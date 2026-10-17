"""Synthetic data generator: determinism and ground-truth properties the evals rely on."""

from __future__ import annotations

from collections import Counter

from eval.synth.generator import generate, lexicon_coverage
from eval.synth.personas import PERSONAS
from finpilot.categories.taxonomy import load_taxonomy


def test_seeded_and_deterministic():
    a, b = generate(PERSONAS[0]), generate(PERSONAS[0])
    assert [t.as_row() for t in a.txns] == [t.as_row() for t in b.txns]


def test_persona_coverage_requirements():
    assert len(PERSONAS) >= 6
    assert {p.reporting_currency for p in PERSONAS} == {"INR", "USD"}
    assert any(p.custom_categories for p in PERSONAS) and any(p.injections for p in PERSONAS)
    for p in PERSONAS:
        ds = generate(p)
        months = {(t.date.year, t.date.month) for t in ds.txns}
        assert 12 <= len(months) <= 18
        assert {t.split for t in ds.txns} == {"history", "holdout"}
        assert ds.series and all(s.txn_ids for s in ds.series)
        assert {a.type for a in ds.anomalies} & {"duplicate", "spike"}
        assert all(d.weekday() < 5 for d in ds.fx)  # business days only


def test_truth_labels_are_valid_categories():
    slugs = {e.slug for e in load_taxonomy()}
    for p in PERSONAS:
        custom = {c["name"] for c in p.custom_categories}
        for t in generate(p).txns:
            assert t.category in slugs | custom, (p.name, t.category)


def test_unseen_and_ambiguous_shares():
    from finpilot.categories.rules import match
    from finpilot.ingest.normalizer import normalize_merchant

    known: dict[str, bool] = {}
    amb = Counter()
    for p in PERSONAS:
        ds = generate(p)
        for t in ds.txns:
            hit = match(normalize_merchant(t.description), t.description, 1 if t.amount > 0 else -1)
            known[t.merchant_key] = known.get(t.merchant_key, False) or bool(
                hit and hit.confidence >= 0.85
            )
            amb[t.ambiguous] += 1
        assert 0 < lexicon_coverage(ds) < 1
    unseen = sum(not v for v in known.values()) / len(known)
    assert unseen >= 0.30, f"only {unseen:.0%} of merchants are unseen by the rule lexicon"
    assert 0.08 <= amb[True] / sum(amb.values()) <= 0.25


def test_fx_persona_has_foreign_currency_rows():
    ds = generate(next(p for p in PERSONAS if p.name == "inr_with_usd_subscriptions"))
    assert {t.currency for t in ds.txns} == {"INR", "USD"}
