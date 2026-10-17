"""Subscription audit over detected recurring expenses (deterministic).

A subscription is an active/lapsed detected expense series whose category is in
`thresholds.yml: subscriptions.categories`, or a fixed-price (CV ≤ 0.10) monthly/quarterly/yearly
charge in a 'discretionary' category. Flags:
- `price_hike`: the series' price history contains a rise ≥ price_hike_min_pct;
- `overlap`: ≥ 2 active subscriptions in one group of config/subscriptions.yml;
- `low_value_proxy`: the user rated usage 0–1 (basis `user_rating`), or, unrated, it costs
  ≥ low_value_cost_multiple × the median subscription (basis `heuristic`).
"Unused" is never asserted: usage is not observable from bank data.
"""

from __future__ import annotations

import re
import statistics
from decimal import Decimal
from functools import lru_cache
from typing import Any

import yaml

from finpilot.config import get_settings
from finpilot.insights.frame import Frame, cross_rate
from finpilot.insights.models import Finding, Subscription
from finpilot.recurring.detect import monthly_equivalent

SUBSCRIPTION_MAX_CV = 0.10


@lru_cache
def overlap_groups() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(
        (get_settings().config_dir / "subscriptions.yml").read_text(encoding="utf-8")
    )
    return {
        k: {
            "label": v["label"],
            "re": re.compile("|".join(f"(?:{p})" for p in v["patterns"]), re.I),
        }
        for k, v in raw["overlap_groups"].items()
    }


def group_of(merchant: str) -> str | None:
    return next((k for k, g in overlap_groups().items() if g["re"].search(merchant)), None)


def is_subscription(d: dict[str, Any], cats: set[str]) -> bool:
    """Subscription categories, or a fixed-price monthly/quarterly/yearly charge in one of the
    USER'S OWN discretionary categories (a custom "Gym"). System categories such as Other or
    Shopping never qualify by heuristic: a monthly remittance to parents is not a subscription,
    and frequent variable bookings (train tickets) are not either."""
    if d["kind"] != "expense":
        return False
    if d.get("category_slug") in cats:
        return True
    custom = d.get("category_id") is not None and d.get("category_slug") is None
    return (
        custom
        and d.get("category_nature") == "discretionary"
        and d["period"] in ("monthly", "quarterly", "yearly")
        and float(d["amount_cv"]) <= SUBSCRIPTION_MAX_CV
    )


def audit(fr: Frame) -> tuple[list[Subscription], dict[str, Any], list[Finding]]:
    cfg = fr.cfg["subscriptions"]
    hike_pct = float(fr.cfg["anomalies"]["price_hike_min_pct"])
    cats = set(cfg["categories"])
    subs: list[Subscription] = []
    for d in fr.detected:
        if d["status"] not in ("active", "lapsed") or not is_subscription(d, cats):
            continue
        ids = [str(i) for i in d["txn_ids_json"]]
        rate = cross_rate(fr, ids)
        monthly = fr.money(float(monthly_equivalent(d["amount_typical"], d["period"])) * rate)
        hikes = [h for h in d["price_history_json"][1:] if float(h.get("pct", 0)) >= hike_pct]
        subs.append(
            Subscription(
                id=str(d["id"]),
                merchant=d["merchant_norm"],
                description=d["description"],
                category_id=str(d["category_id"]) if d["category_id"] else None,
                category_name=d.get("category_name"),
                period=d["period"],
                amount=d["amount_typical"],
                currency=d["currency"],
                monthly_cost=monthly,
                annual_cost=fr.money(float(monthly) * 12),
                reporting_currency=fr.currency,
                n_charges=d["n_occurrences"],
                first_seen=d["first_seen"],
                last_seen=d["last_seen"],
                next_expected=d["next_expected"],
                status=d["status"],
                usage_rating=d["usage_rating"],
                flags=["price_hike"] if hikes else [],
                overlap_group=group_of(d["merchant_norm"]),
                price_history=d["price_history_json"],
                declared_rule_id=str(d["promoted_to_rule_id"])
                if d["promoted_to_rule_id"]
                else None,
                evidence_txn_ids=ids[-6:],
            )
        )
    findings: list[Finding] = []
    active = [s for s in subs if s.status == "active"]
    # overlap
    groups: dict[str, list[Subscription]] = {}
    for s in active:
        if s.overlap_group:
            groups.setdefault(s.overlap_group, []).append(s)
    for g, members in sorted(groups.items()):
        if len({m.merchant for m in members}) < 2:
            continue
        names = [m.merchant for m in members]
        for m in members:
            m.flags.append("overlap")
            m.overlap_with = [n for n in names if n != m.merchant]
        total = sum((m.monthly_cost for m in members), Decimal(0))
        findings.append(
            Finding(
                fingerprint=f"subscription_overlap:{g}:{','.join(sorted(m.id for m in members))}",
                kind="subscription",
                type="subscription_overlap",
                severity="medium" if total >= fr.thr.min_anomaly_amount / 4 else "low",
                metric="subscriptions_in_group",
                baseline=Decimal(1),
                observed=Decimal(len(members)),
                threshold=Decimal(2),
                unit="count",
                currency=fr.currency,
                evidence_txn_ids=[i for m in members for i in m.evidence_txn_ids[-2:]],
                data={
                    "group": g,
                    "group_label": overlap_groups()[g]["label"],
                    "names": " and ".join([", ".join(names[:-1]), names[-1]])
                    if len(names) > 1
                    else names[0],
                    "monthly_total": total,
                    "currency": fr.currency,
                    "subscription_ids": [m.id for m in members],
                },
            )
        )
    # low value
    median = statistics.median([float(s.monthly_cost) for s in active]) if active else 0.0
    multiple = float(cfg["low_value_cost_multiple"])
    for s in active:
        basis = None
        if s.usage_rating is not None and s.usage_rating <= int(cfg["rating_low_max"]):
            basis = "user_rating"
        elif (
            s.usage_rating is None
            and len(active) >= 3
            and median > 0
            and float(s.monthly_cost) >= multiple * median
        ):
            basis = "heuristic"
        if basis is None:
            continue
        s.flags.append("low_value_proxy")
        s.low_value_basis = basis
        findings.append(
            Finding(
                fingerprint=f"subscription_low_value:{s.id}:{basis}",
                kind="subscription",
                type="subscription_low_value",
                severity="medium" if basis == "user_rating" else "low",
                metric="usage_rating" if basis == "user_rating" else "cost_vs_median_subscription",
                baseline=None if basis == "user_rating" else fr.money(median),
                observed=Decimal(s.usage_rating or 0) if basis == "user_rating" else s.monthly_cost,
                threshold=Decimal(cfg["rating_low_max"])
                if basis == "user_rating"
                else fr.money(multiple * median),
                unit="count" if basis == "user_rating" else "currency",
                currency=fr.currency,
                evidence_txn_ids=s.evidence_txn_ids[-3:],
                data={
                    "merchant": s.merchant,
                    "basis": basis,
                    "rating": s.usage_rating,
                    "monthly": s.monthly_cost,
                    "annual": s.annual_cost,
                    "median": fr.money(median),
                    "multiple": round(float(s.monthly_cost) / median, 1) if median else None,
                    "currency": fr.currency,
                    "subscription_id": s.id,
                },
            )
        )
    subs.sort(key=lambda s: (s.status != "active", -float(s.monthly_cost)))
    totals = {
        "count": len(active),
        "monthly": sum((s.monthly_cost for s in active), Decimal(0)),
        "annual": sum((s.annual_cost for s in active), Decimal(0)),
        "currency": fr.currency,
        "flagged": sum(1 for s in active if s.flags),
    }
    return subs, totals, findings
