"""Rule-lexicon matcher (config/rules_lexicon.yml): deterministic categorizer layer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import yaml

from finpilot.config import get_settings

Sign = Literal["debit", "credit", "any"]


@dataclass(frozen=True)
class Rule:
    category_slug: str
    pattern: re.Pattern[str]
    sign: Sign
    confidence: float


@dataclass(frozen=True)
class RuleHit:
    category_slug: str
    confidence: float
    pattern: str


@lru_cache
def load_rules() -> tuple[Rule, ...]:
    raw = yaml.safe_load(
        (get_settings().config_dir / "rules_lexicon.yml").read_text(encoding="utf-8")
    )
    out = []
    for r in raw["rules"]:
        for p in r["patterns"]:
            out.append(
                Rule(
                    r["category"],
                    re.compile(p, re.IGNORECASE),
                    r.get("sign", "any"),
                    float(r["confidence"]),
                )
            )
    return tuple(out)


@lru_cache
def thresholds() -> dict:
    return yaml.safe_load(
        (get_settings().config_dir / "thresholds.yml").read_text(encoding="utf-8")
    )


def match(merchant_norm: str, raw_description: str, amount_sign: int) -> RuleHit | None:
    """Best rule hit for a transaction (amount_sign: -1 debit, +1 credit)."""
    hay = f"{merchant_norm or ''} || {(raw_description or '').upper()}"
    best: RuleHit | None = None
    best_len = -1
    for r in load_rules():
        if (r.sign == "debit" and amount_sign > 0) or (r.sign == "credit" and amount_sign < 0):
            continue
        m = r.pattern.search(hay)
        if not m:
            continue
        span = m.end() - m.start()
        if (
            best is None
            or r.confidence > best.confidence
            or (r.confidence == best.confidence and span > best_len)
        ):
            best, best_len = RuleHit(r.category_slug, r.confidence, r.pattern.pattern), span
    return best
