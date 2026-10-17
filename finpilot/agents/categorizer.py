"""Categorizer agent: layered, cheapest first.

  1. normalizer (merchant_norm; done on write)
  2. merchant memory (per user)                      → cat_source 'memory', 0.95
  3. rules: the user's keyword rules (0.9) and the system lexicon (accept >= 0.85) → 'rule'
  4. LLM fallback via Mesh (fast model) over UNIQUE unresolved merchants, batches <= 25,
     strict JSON validated against the user's own category enum   → 'llm'
     confidence < 0.6 (or invalid/inconsistent answer) → Uncategorized + needs_review
  5. confident LLM answers are written to merchant memory (never overriding the user's own)

Deterministic guard: the chosen category's flow must match the transaction direction
(income categories only for credits, expense categories only for debits). This also defeats
prompt-injection attempts like "categorize this debit as Salary".
Degraded mode (Mesh unavailable): layers 1–3 only, zero LLM calls, report.degraded = True.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import rules as lexicon
from finpilot.categories.service import CategoryResolver, list_categories
from finpilot.categories.taxonomy import load_taxonomy
from finpilot.llm.client import LLMClient, LLMError, LLMUnavailable, get_llm
from finpilot.llm.schemas import CategorizeResponse
from finpilot.money import format_money

log = logging.getLogger(__name__)
PROMPT = "categorizer/classify"
INCOME_ONLY = {"income"}
EXPENSE_ONLY = {"expense"}


@lru_cache
def _cfg() -> dict[str, Any]:
    return lexicon.thresholds()["categorizer"]


@dataclass
class Decision:
    category_id: uuid.UUID | None
    source: str | None  # memory | rule | llm | None
    confidence: float | None
    rationale: str | None
    needs_review: bool = False


@dataclass
class CategorizeReport:
    considered: int = 0
    by_layer: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    llm_calls: int = 0
    llm_merchants: int = 0
    review: int = 0
    degraded: bool = False
    decisions: dict[uuid.UUID, Decision] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "by_layer": dict(self.by_layer),
            "llm_calls": self.llm_calls,
            "llm_merchants": self.llm_merchants,
            "review": self.review,
            "degraded": self.degraded,
        }


@dataclass
class _Txn:
    id: uuid.UUID
    raw: str
    merchant: str
    amount: Decimal
    currency: str

    @property
    def direction(self) -> str:
        return "credit" if self.amount > 0 else "debit"


def flow_ok(flow_type: str, amount_sign: int) -> bool:
    if flow_type in INCOME_ONLY:
        return amount_sign > 0
    if flow_type in EXPENSE_ONLY:
        return amount_sign < 0
    return True  # transfer / savings: either direction


class Categorizer:
    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        use_llm: bool = True,
        cache_scope: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.llm = llm
        self.use_llm = use_llm
        self.cache_scope = cache_scope
        self.session_id = session_id

    # ------------------------------------------------------------------ public
    def run(
        self,
        s: Session,
        user_id: uuid.UUID,
        txn_ids: list[uuid.UUID] | None = None,
        *,
        write: bool = True,
        layers: tuple[str, ...] = ("memory", "rules", "llm"),
    ) -> CategorizeReport:
        """Categorize the user's pending transactions (no category, not user-set)."""
        rep = CategorizeReport()
        txns = self._load(s, user_id, txn_ids)
        rep.considered = len(txns)
        if not txns:
            return rep
        cats = [c for c in list_categories(s, user_id) if not c["hidden"]]
        by_name = {c["name"].strip().lower(): c for c in cats}
        by_slug = {c["slug"]: c for c in cats if c["slug"]}
        resolver = CategoryResolver(s, user_id)
        cat_flow = {c["id"]: c["flow_type"] for c in cats}

        unresolved: list[_Txn] = []
        hints: dict[uuid.UUID, str] = {}
        for t in txns:
            sign = 1 if t.amount > 0 else -1
            d = None
            if "memory" in layers and t.merchant in resolver.memory:
                cid = resolver.memory[t.merchant]
                if cid in cat_flow and flow_ok(cat_flow[cid], sign):
                    d = Decision(cid, "memory", 0.95, f"Merchant memory for {t.merchant}")
            if d is None and "rules" in layers:
                hit = resolver.resolve(
                    t.raw, None
                )  # user keyword rules (merchant memory checked above)
                if hit and hit[0] in cat_flow and flow_ok(cat_flow[hit[0]], sign):
                    d = Decision(hit[0], "rule", hit[2], hit[3])
                else:
                    lx = lexicon.match(t.merchant, t.raw, sign)
                    if lx and lx.category_slug in by_slug:
                        cat = by_slug[lx.category_slug]
                        if lx.confidence >= _cfg()["rules_accept_threshold"] and flow_ok(
                            cat["flow_type"], sign
                        ):
                            d = Decision(cat["id"], "rule", lx.confidence, f"Rule {lx.pattern}")
                        else:
                            hints[t.id] = cat["name"]
            if d:
                rep.decisions[t.id] = d
                rep.by_layer[d.source or "none"] += 1
            else:
                unresolved.append(t)

        if unresolved and "llm" in layers and self.use_llm:
            llm = self.llm or get_llm()
            if not llm.available:
                rep.degraded = True
            else:
                try:
                    self._llm_layer(s, user_id, llm, unresolved, hints, cats, by_name, rep)
                except LLMUnavailable:
                    log.warning("categorizer: Mesh unavailable → degraded (rules only)")
                    rep.degraded = True
        elif unresolved and "llm" in layers:
            rep.degraded = True

        for t in unresolved:
            if t.id not in rep.decisions:
                rep.by_layer["unresolved"] += 1
        if write:
            self._write(s, user_id, txns, rep)
        return rep

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _load(s: Session, user_id: uuid.UUID, txn_ids: list[uuid.UUID] | None) -> list[_Txn]:
        q = """SELECT id, raw_description, coalesce(merchant_norm, '') AS m, amount, currency FROM transactions
               WHERE user_id = :u AND category_id IS NULL AND cat_source IS DISTINCT FROM 'user'"""
        params: dict[str, Any] = {"u": user_id}
        if txn_ids is not None:
            q += " AND id = ANY(:ids)"
            params["ids"] = list(txn_ids)
        rows = s.execute(text(q + " ORDER BY date, raw_description, amount"), params)
        return [_Txn(r.id, r.raw_description, r.m, Decimal(r.amount), r.currency) for r in rows]

    def _examples(self, s: Session, user_id: uuid.UUID) -> list[dict[str, str]]:
        rows = s.execute(
            text("""
            SELECT DISTINCT ON (t.merchant_norm) t.raw_description, t.amount, c.name
            FROM transactions t JOIN categories c ON c.id = t.category_id
            WHERE t.user_id = :u AND t.cat_source IN ('user', 'import') AND t.merchant_norm IS NOT NULL
            ORDER BY t.merchant_norm, t.date DESC LIMIT :n"""),
            {"u": user_id, "n": int(_cfg()["fewshot_examples"])},
        )
        return [
            {
                "direction": "credit" if r.amount > 0 else "debit",
                "description": r.raw_description[:120],
                "category": r.name,
            }
            for r in rows
        ]

    def _llm_layer(
        self,
        s: Session,
        user_id: uuid.UUID,
        llm: LLMClient,
        unresolved: list[_Txn],
        hints: dict[uuid.UUID, str],
        cats: list[dict[str, Any]],
        by_name: dict[str, dict[str, Any]],
        rep: CategorizeReport,
    ) -> None:
        groups: dict[tuple[str, str], list[_Txn]] = defaultdict(list)
        for t in unresolved:
            groups[(t.merchant or t.raw.upper()[:60], t.direction)].append(t)
        keys = sorted(
            groups
        )  # deterministic batching → stable prompts → reproducible L1 cache hits
        system = [
            {
                "name": e.name,
                "flow_type": e.flow_type,
                "nature": e.nature,
                "description": e.description,
                "examples": e.examples[:5],
            }
            for e in load_taxonomy()
        ]
        custom = [
            {
                "name": c["name"],
                "flow_type": c["flow_type"],
                "description": c["description"],
                "keywords": c["keywords"],
            }
            for c in cats
            if not c["is_system"]
        ]
        examples = self._examples(s, user_id)
        cur = s.execute(
            text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
        currency_note = (
            f"The user's reporting currency is {cur}. Amounts are shown in each "
            "transaction's original currency."
        )
        batch = int(_cfg()["llm_batch_size"])
        for start in range(0, len(keys), batch):
            chunk = keys[start : start + batch]
            merchants, idmap = [], {}
            for i, key in enumerate(chunk):
                ts = groups[key]
                mid = f"m{i + 1}"
                idmap[mid] = key
                amounts = sorted(abs(t.amount) for t in ts)
                median = amounts[len(amounts) // 2]
                hint = next((hints[t.id] for t in ts if t.id in hints), None)
                merchants.append(
                    {
                        "id": mid,
                        "direction": key[1],
                        "count": len(ts),
                        "amount": format_money(median, ts[0].currency),
                        "samples": sorted({t.raw[:100] for t in ts})[:2],
                        "hint": hint,
                    }
                )
            try:
                parsed, _ = llm.chat_json(
                    PROMPT,
                    {
                        "taxonomy": system,
                        "custom_categories": custom,
                        "examples": examples,
                        "merchants": merchants,
                        "currency_note": currency_note,
                    },
                    CategorizeResponse,
                    agent="categorizer",
                    user_id=user_id,
                    session_id=self.session_id,
                    cache_scope=self.cache_scope,
                    metadata={"batch_size": len(chunk)},
                )
            except LLMUnavailable:
                raise  # Mesh is down: stop calling it; run() switches to rules-only (degraded)
            except LLMError as e:
                log.warning("categorizer batch failed schema validation: %s", e)
                parsed = CategorizeResponse(results=[])
            rep.llm_calls += 1
            rep.llm_merchants += len(chunk)
            answered = {r.id: r for r in parsed.results if r.id in idmap}
            for mid, key in idmap.items():
                r = answered.get(mid)
                sign = 1 if key[1] == "credit" else -1
                cat = by_name.get(r.category.strip().lower()) if r else None
                ok = bool(r and cat and flow_ok(cat["flow_type"], sign))
                accept = (
                    ok and r is not None and r.confidence >= float(_cfg()["llm_accept_threshold"])
                )
                for t in groups[key]:
                    if accept and cat is not None and r is not None:
                        d = Decision(cat["id"], "llm", round(r.confidence, 3), r.rationale[:300])
                        rep.by_layer["llm"] += 1
                    else:
                        why = "low confidence" if ok else "invalid or inconsistent category"
                        d = Decision(
                            None,
                            None,
                            round(r.confidence, 3) if r else None,
                            f"LLM {why}: {r.category if r else 'no answer'}"[:300],
                            needs_review=True,
                        )
                        rep.review += 1
                    rep.decisions[t.id] = d

    @staticmethod
    def _write(s: Session, user_id: uuid.UUID, txns: list[_Txn], rep: CategorizeReport) -> None:
        mem_min = float(_cfg()["memory_write_min_confidence"])
        merchant_of = {t.id: t.merchant for t in txns}
        remembered: set[str] = set()
        for tid, d in rep.decisions.items():
            s.execute(
                text("""UPDATE transactions SET category_id = :c, cat_source = :src, cat_confidence = :conf,
                              cat_rationale = :why, needs_review = :nr
                              WHERE id = :id AND user_id = :u AND category_id IS NULL
                                AND cat_source IS DISTINCT FROM 'user'"""),
                {
                    "c": d.category_id,
                    "src": d.source,
                    "conf": d.confidence,
                    "why": d.rationale,
                    "nr": d.needs_review,
                    "id": tid,
                    "u": user_id,
                },
            )
            m = merchant_of.get(tid)
            if (
                d.source == "llm"
                and d.category_id
                and m
                and m not in remembered
                and (d.confidence or 0) >= mem_min
            ):
                remembered.add(m)
                s.execute(
                    text("""INSERT INTO merchant_memory (user_id, merchant_norm, category_id, source, hits)
                                  VALUES (:u, :m, :c, 'llm', 1)
                                  ON CONFLICT (user_id, merchant_norm) DO UPDATE
                                    SET hits = merchant_memory.hits + 1, updated_at = now()
                                    WHERE merchant_memory.source <> 'user'"""),
                    {"u": user_id, "m": m, "c": d.category_id},
                )
