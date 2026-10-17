"""Recurring-series DETECTION from transaction history (user-declared rules live in service.py).

Pipeline (deterministic, no LLM):
1. `recurring_candidates.sql` groups charges by (merchant_norm, direction, original currency).
2. `merge_candidates` joins merchant spellings that are the same payee (bank typos, branch
   suffixes): identical letters per word, or a difflib ratio ≥ `merchant_merge_similarity`.
3. `fit_series` tries every period (weekly … yearly): it builds a chain of charges at least half a
   period apart (extra charges such as duplicates are set aside, keeping the one whose amount fits
   the series), then requires the median-free regularity test: ≥ `regular_share` of the gaps
   within ± tolerance of 1 period (or 2 = one missed charge), ≥ 3 charges (≥ 2 yearly) and an
   amount CV ≤ `max_amount_cv`. The best-scoring period wins (regularity × share of charges kept).
   Groups that fail as a whole but have ≥ 6 charges are retried per amount cluster (one merchant,
   several subscriptions).
4. Price history: change points on fixed-amount series; a rise ≥ `price_hike_min_pct` is a hike.
5. `refresh` upserts `detected_recurrences` (keeping the user's dismiss / rating / promotion) and
   links declared rules (same kind + currency, same name, or same period with amount within 10%).
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from itertools import pairwise
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories.rules import thresholds
from finpilot.db.sql_loader import sql
from finpilot.ingest.normalizer import normalize_merchant
from finpilot.recurring.rules import _add_months

MONTH_STEPS = {"monthly": 1, "quarterly": 3, "yearly": 12}
CENT = Decimal("0.01")


@dataclass(frozen=True)
class DetectConfig:
    min_occurrences: int = 3
    min_occurrences_yearly: int = 2
    regular_share: float = 0.70
    max_amount_cv: float = 0.35
    fixed_amount_cv: float = 0.05
    merge_similarity: float = 0.88
    lapsed_after_periods: float = 1.6
    price_hike_min_pct: float = 5.0
    max_extra_share: float = 0.34
    yearly_max_amount_cv: float = 0.10
    periods: tuple[tuple[str, float, float], ...] = (
        ("weekly", 7.0, 1.0),
        ("biweekly", 14.0, 2.0),
        ("monthly", 30.44, 6.0),
        ("quarterly", 91.3, 10.0),
        ("yearly", 365.25, 20.0),
    )

    @classmethod
    def load(cls) -> DetectConfig:
        return _load_config()

    def nominal(self, period: str) -> tuple[float, float]:
        return next((d, t) for p, d, t in self.periods if p == period)


@lru_cache
def _load_config() -> DetectConfig:
    t = thresholds()
    r = t.get("recurring_detection", {})
    a = t.get("anomalies", {})
    return DetectConfig(
        min_occurrences=int(r.get("min_occurrences", 3)),
        min_occurrences_yearly=int(r.get("min_occurrences_yearly", 2)),
        regular_share=float(r.get("regular_share", 0.7)),
        max_amount_cv=float(r.get("max_amount_cv", 0.35)),
        fixed_amount_cv=float(r.get("fixed_amount_cv", 0.05)),
        merge_similarity=float(r.get("merchant_merge_similarity", 0.88)),
        lapsed_after_periods=float(r.get("lapsed_after_periods", 1.6)),
        price_hike_min_pct=float(a.get("price_hike_min_pct", 5)),
        max_extra_share=float(r.get("max_extra_share", 0.34)),
        yearly_max_amount_cv=float(r.get("yearly_max_amount_cv", 0.10)),
        periods=tuple(
            (name, float(v[0]), float(v[1])) for name, v in (r.get("periods") or {}).items()
        )
        or DetectConfig.periods,
    )


@dataclass(frozen=True)
class Occ:
    id: str
    date: dt.date
    amount: Decimal  # positive, original currency


@dataclass
class Candidate:
    merchant_norm: str
    kind: str  # expense | income
    currency: str
    occ: list[Occ]
    last_description: str = ""
    category_id: str | None = None
    rule_id: str | None = None
    names: set[str] = field(default_factory=set)


@dataclass
class Fit:
    period: str
    period_days: float
    regular_share: float
    amount_mean: Decimal
    amount_typical: Decimal
    amount_last: Decimal
    amount_cv: float
    first_seen: dt.date
    last_seen: dt.date
    next_expected: dt.date
    status: str  # active | lapsed
    confidence: float
    txn_ids: list[str]
    extra_ids: list[str]
    price_history: list[dict[str, Any]]
    hikes: list[dict[str, Any]]

    @property
    def n(self) -> int:
        return len(self.txn_ids)


@dataclass
class Detected:
    series_key: str
    cand: Candidate
    fit: Fit
    rule_id: str | None = None


def _q(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------- fitting
def _chain(occ: list[Occ], days: float) -> tuple[list[Occ], list[Occ]]:
    """Charges ≥ half a period apart; of two close charges keep the one nearer the series level."""
    kept: list[Occ] = [occ[0]]
    extras: list[Occ] = []
    for o in occ[1:]:
        if (o.date - kept[-1].date).days >= 0.5 * days:
            kept.append(o)
            continue
        ref = kept[-4:-1] or kept[-1:]
        level = statistics.median(float(k.amount) for k in ref)
        if len(kept) > 1 and abs(float(o.amount) - level) < abs(float(kept[-1].amount) - level):
            extras.append(kept[-1])
            kept[-1] = o
        else:
            extras.append(o)
    return kept, extras


def _regularity(kept: list[Occ], days: float, tol: float) -> tuple[float, float]:
    gaps = [(b.date - a.date).days for a, b in pairwise(kept)]
    if not gaps:
        return 0.0, 0.0
    fits = singles = 0
    for g in gaps:
        k = max(1, round(g / days))
        if k <= 2 and abs(g - k * days) <= tol * k:
            fits += 1
            singles += k == 1
    return fits / len(gaps), singles / len(gaps)


def _next_expected(kept: list[Occ], period: str, days: float) -> dt.date:
    """Next DUE date: step from the last charge's due date (a bill due on the 31st that posted
    on the 3rd is not due again on the 31st of the following month)."""
    last = kept[-1].date
    if period in MONTH_STEPS:
        dom = int(statistics.median_high(o.date.day for o in kept[-3:]))
        due = _add_months(last, 0, dom)
        if due > last:
            due = _add_months(last, -1, dom)
        nxt = _add_months(due, MONTH_STEPS[period], dom)
        _, tol = DetectConfig().nominal(period)
        if abs((nxt - last).days - days) <= tol:
            return nxt
        # posting delays blurred the day of month: step by the recent typical gap instead
        gaps = [(b.date - a.date).days for a, b in pairwise(kept[-4:])]
        return last + dt.timedelta(days=int(statistics.median(gaps)) if gaps else round(days))
    return last + dt.timedelta(days=round(days))


def _price_history(kept: list[Occ], cfg: DetectConfig) -> tuple[list[dict], list[dict]]:
    """Change points on fixed-amount stretches (≥ 2% move that persists, or is the latest)."""
    amounts = [float(o.amount) for o in kept]
    history: list[dict[str, Any]] = [
        {"date": kept[0].date.isoformat(), "amount": str(kept[0].amount), "txn_id": kept[0].id}
    ]
    hikes: list[dict] = []
    for i in range(1, len(kept)):
        prior = amounts[max(0, i - 6) : i]
        base = statistics.median(amounts[max(0, i - 3) : i])
        if base <= 0:
            continue
        pct = (amounts[i] - base) / base * 100
        stable = (
            len(prior) >= 3
            and statistics.pstdev(prior) / statistics.mean(prior) <= cfg.fixed_amount_cv
        )
        persists = i == len(kept) - 1 or abs(amounts[i + 1] - amounts[i]) / amounts[i] <= 0.02
        if abs(pct) >= 2 and stable and persists:
            point: dict[str, Any] = {
                "date": kept[i].date.isoformat(),
                "amount": str(kept[i].amount),
                "from": str(_q(base)),
                "pct": round(pct, 2),
                "txn_id": kept[i].id,
            }
            history.append(point)
            if pct >= cfg.price_hike_min_pct:
                hikes.append(point)
    return history, hikes


def fit_series(occ: list[Occ], as_of: dt.date, cfg: DetectConfig | None = None) -> Fit | None:
    cfg = cfg or DetectConfig.load()
    occ = sorted(occ, key=lambda o: (o.date, o.id))
    if len(occ) < min(cfg.min_occurrences, cfg.min_occurrences_yearly):
        return None
    best: tuple[float, str, float, float, list[Occ], list[Occ]] | None = None
    for period, days, tol in cfg.periods:
        kept, extras = _chain(occ, days)
        need = cfg.min_occurrences_yearly if period == "yearly" else cfg.min_occurrences
        if len(kept) < need:
            continue
        share, singles = _regularity(kept, days, tol)
        if share < cfg.regular_share or singles < 0.5:
            continue
        if len(extras) > cfg.max_extra_share * len(kept):
            continue  # a frequent merchant (coffee, groceries): a sparse sub-chain is not a series
        amounts = [float(o.amount) for o in kept]
        cv = statistics.pstdev(amounts) / statistics.mean(amounts)
        if cv > (cfg.yearly_max_amount_cv if period == "yearly" else cfg.max_amount_cv):
            continue
        score = share * len(kept) / len(occ)
        if best is None or score > best[0] + 1e-9:
            best = (score, period, days, share, kept, extras)
    if best is None:
        return None
    _, period, days, share, kept, extras = best
    amounts = [float(o.amount) for o in kept]
    mean = statistics.mean(amounts)
    cv = statistics.pstdev(amounts) / mean
    nxt = _next_expected(kept, period, days)
    _, tol = cfg.nominal(period)
    lapsed = (as_of - kept[-1].date).days > cfg.lapsed_after_periods * days + tol
    need = cfg.min_occurrences_yearly if period == "yearly" else cfg.min_occurrences
    confidence = (
        0.55 * share
        + 0.25 * min(len(kept) / (need * 3), 1.0)
        + 0.20 * (1 - min(cv / cfg.max_amount_cv, 1.0))
    )
    history, hikes = _price_history(kept, cfg)
    return Fit(
        period=period,
        period_days=days,
        regular_share=round(share, 4),
        amount_mean=_q(mean),
        amount_typical=_q(statistics.median(amounts[-3:])),
        amount_last=kept[-1].amount,
        amount_cv=round(cv, 4),
        first_seen=kept[0].date,
        last_seen=kept[-1].date,
        next_expected=nxt,
        status="lapsed" if lapsed else "active",
        confidence=round(min(confidence, 1.0), 3),
        txn_ids=[o.id for o in kept],
        extra_ids=[o.id for o in extras],
        price_history=history,
        hikes=hikes,
    )


def monthly_equivalent(amount: Decimal, period: str) -> Decimal:
    per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}[period]
    return _q(amount * per_year / 12)


# ---------------------------------------------------------------------------- grouping
def canonical(name: str) -> str:
    """Same letters per word → same key (catches adjacent-letter swaps like SPTOIFY)."""
    return " ".join("".join(sorted(w)) for w in name.split())


def _similar(a: str, b: str, threshold: float) -> bool:
    if canonical(a) == canonical(b):
        return True
    if abs(len(a) - len(b)) > 3:
        return False
    sm = difflib.SequenceMatcher(None, a, b)
    return (
        sm.real_quick_ratio() >= threshold
        and sm.quick_ratio() >= threshold
        and sm.ratio() >= threshold
    )


def _union(group: list[Candidate], threshold: float) -> list[list[Candidate]]:
    """Union-find over one (kind, currency) bucket sorted largest first; members keep that order."""
    parent = list(range(len(group)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    anchors = [i for i, c in enumerate(group) if len(c.occ) >= 2]
    for i in range(len(group)):
        for j in anchors:
            if j >= i and len(group[i].occ) >= 2:
                break  # anchor pairs are checked once (j < i)
            if j == i or find(i) == find(j):
                continue
            if _similar(group[i].merchant_norm, group[j].merchant_norm, threshold):
                parent[find(i)] = find(j)
                if len(group[i].occ) < 2:
                    break  # a singleton joins the largest similar group
    merged: dict[int, list[Candidate]] = defaultdict(list)
    for i, c in enumerate(group):
        merged[find(i)].append(c)
    return list(merged.values())


def merge_candidates(cands: list[Candidate], threshold: float) -> list[Candidate]:
    """Union same-payee spellings within (kind, currency). Singletons only join a larger group."""
    out: list[Candidate] = []
    buckets: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for c in cands:
        buckets[(c.kind, c.currency)].append(c)
    for group in buckets.values():
        group.sort(key=lambda c: (-len(c.occ), c.merchant_norm))
        for members in _union(group, threshold):
            head = members[0]  # largest (sorted above)
            occ = sorted((o for m in members for o in m.occ), key=lambda o: (o.date, o.id))
            if len(occ) < 2:
                continue
            latest = max(members, key=lambda m: max(o.date for o in m.occ))
            out.append(
                Candidate(
                    merchant_norm=head.merchant_norm,
                    kind=head.kind,
                    currency=head.currency,
                    occ=occ,
                    last_description=latest.last_description,
                    category_id=head.category_id
                    or next((m.category_id for m in members if m.category_id), None),
                    rule_id=next((m.rule_id for m in members if m.rule_id), None),
                    names={m.merchant_norm for m in members},
                )
            )
    return out


def _amount_clusters(occ: list[Occ]) -> list[list[Occ]]:
    srt = sorted(occ, key=lambda o: o.amount)
    clusters: list[list[Occ]] = [[srt[0]]]
    for a, b in pairwise(srt):
        if float(b.amount) > float(a.amount) * 1.25:
            clusters.append([])
        clusters[-1].append(b)
    return [sorted(c, key=lambda o: (o.date, o.id)) for c in clusters if len(c) >= 2]


def detect(
    cands: list[Candidate], as_of: dt.date, cfg: DetectConfig | None = None
) -> list[Detected]:
    cfg = cfg or DetectConfig.load()
    out: list[Detected] = []
    for c in merge_candidates(cands, cfg.merge_similarity):
        base_key = f"{c.kind}|{c.currency}|{c.merchant_norm}"
        f = fit_series(c.occ, as_of, cfg)
        if f is not None:
            out.append(Detected(base_key, c, f))
            continue
        if len(c.occ) >= 6:
            for cl in _amount_clusters(c.occ):
                f = fit_series(cl, as_of, cfg)
                if f is not None:
                    out.append(Detected(f"{base_key}|~{int(f.amount_typical)}", c, f))
    return sorted(out, key=lambda d: d.series_key)


# ---------------------------------------------------------------------------- declared rules
def match_rules(detected: list[Detected], rules: list[dict[str, Any]]) -> None:
    """Declared rules override detected series: link each rule to at most one series."""
    taken: set[str] = set()
    for r in rules:
        rname = normalize_merchant(r["description"])
        best: tuple[float, Detected] | None = None
        for d in detected:
            if (
                d.series_key in taken
                or d.cand.kind != r["kind"]
                or d.cand.currency != r["currency"]
            ):
                continue
            if d.cand.rule_id == str(r["id"]):
                score = 3.0
            elif rname and (rname in d.cand.names or rname == d.cand.merchant_norm):
                score = 2.0
            else:
                rel = abs(float(d.fit.amount_typical) - float(r["amount"])) / float(r["amount"])
                same_cat = (
                    not r["category_id"]
                    or not d.cand.category_id
                    or str(r["category_id"]) == d.cand.category_id
                )
                if r["frequency"] != d.fit.period or rel > 0.10 or not same_cat:
                    continue
                score = 1.0 - rel
            if best is None or score > best[0]:
                best = (score, d)
        if best:
            best[1].rule_id = str(r["id"])
            taken.add(best[1].series_key)


# ---------------------------------------------------------------------------- persistence
def load_candidates(
    s: Session, user_id: uuid.UUID, as_of: dt.date, since: dt.date | None = None
) -> list[Candidate]:
    rows = s.execute(
        sql("recurring_candidates"),
        {"u": user_id, "as_of": as_of, "since": since or as_of - dt.timedelta(days=3 * 366)},
    ).mappings()
    out = []
    for r in rows:
        occ = [
            Occ(str(i), d, Decimal(a))
            for i, d, a in zip(r["ids"], r["dates"], r["amounts"], strict=True)
        ]
        out.append(
            Candidate(
                merchant_norm=r["merchant_norm"],
                kind=r["kind"],
                currency=r["currency"],
                occ=occ,
                last_description=r["last_description"],
                category_id=str(r["category_id"]) if r["category_id"] else None,
                rule_id=str(r["rule_id"]) if r["rule_id"] else None,
                names={r["merchant_norm"]},
            )
        )
    return out


_UPSERT = text("""
INSERT INTO detected_recurrences (user_id, series_key, merchant_norm, description, kind, currency, category_id,
  period, period_days, amount_mean, amount_typical, amount_last, amount_cv, n_occurrences, first_seen,
  last_seen, next_expected, status, confidence, price_history_json, txn_ids_json, promoted_to_rule_id,
  last_detected_at)
VALUES (:u, :key, :m, :desc, :kind, :cur, :cat, :period, :pdays, :mean, :typ, :last, :cv, :n, :first,
  :lastseen, :next, :status, :conf, CAST(:ph AS jsonb), CAST(:ids AS jsonb), :rule, now())
ON CONFLICT (user_id, series_key) DO UPDATE SET
  merchant_norm = EXCLUDED.merchant_norm, description = EXCLUDED.description,
  category_id = EXCLUDED.category_id, period = EXCLUDED.period, period_days = EXCLUDED.period_days,
  amount_mean = EXCLUDED.amount_mean, amount_typical = EXCLUDED.amount_typical,
  amount_last = EXCLUDED.amount_last, amount_cv = EXCLUDED.amount_cv,
  n_occurrences = EXCLUDED.n_occurrences, first_seen = EXCLUDED.first_seen,
  last_seen = EXCLUDED.last_seen, next_expected = EXCLUDED.next_expected, status = EXCLUDED.status,
  confidence = EXCLUDED.confidence, price_history_json = EXCLUDED.price_history_json,
  txn_ids_json = EXCLUDED.txn_ids_json,
  promoted_to_rule_id = coalesce(EXCLUDED.promoted_to_rule_id, detected_recurrences.promoted_to_rule_id),
  last_detected_at = now()
""")


def refresh(
    s: Session, user_id: uuid.UUID, as_of: dt.date, cfg: DetectConfig | None = None
) -> list[Detected]:
    """Detect series for one user and upsert them; series no longer detected become 'gone'."""
    cfg = cfg or DetectConfig.load()
    found = detect(load_candidates(s, user_id, as_of), as_of, cfg)
    rules = [
        dict(r)
        for r in s.execute(
            text("""SELECT id, kind, description, amount, currency, frequency, category_id
                    FROM recurring_rules WHERE user_id = :u AND status <> 'ended' ORDER BY created_at"""),
            {"u": user_id},
        ).mappings()
    ]
    match_rules(found, rules)
    for d in found:
        f = d.fit
        s.execute(
            _UPSERT,
            {
                "u": user_id,
                "key": d.series_key[:300],
                "m": d.cand.merchant_norm[:200],
                "desc": (d.cand.last_description or d.cand.merchant_norm)[:500],
                "kind": d.cand.kind,
                "cur": d.cand.currency,
                "cat": d.cand.category_id,
                "period": f.period,
                "pdays": round(f.period_days, 2),
                "mean": f.amount_mean,
                "typ": f.amount_typical,
                "last": f.amount_last,
                "cv": f.amount_cv,
                "n": f.n,
                "first": f.first_seen,
                "lastseen": f.last_seen,
                "next": f.next_expected,
                "status": f.status,
                "conf": f.confidence,
                "ph": json.dumps(f.price_history),
                "ids": json.dumps(f.txn_ids),
                "rule": d.rule_id,
            },
        )
    s.execute(
        text("""UPDATE detected_recurrences SET status = 'gone'
                WHERE user_id = :u AND status <> 'gone' AND NOT (series_key = ANY(:keys))"""),
        {"u": user_id, "keys": [d.series_key[:300] for d in found]},
    )
    return found
