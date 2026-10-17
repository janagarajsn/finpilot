"""Intent router: question → (intent, period). Cheapest layer first:

1. rules: regex patterns per intent (config/intents.yml, file order; first match wins)
2. embedding similarity to each intent's example phrasings (only when Mesh embeddings work;
   example vectors are cached in Redis per embedding model)
3. fast-LLM classification (prompts/intent_router/classify.v1.md), validated against the known
   intents
4. default: free_form

The question vector (when computed) is returned so the L3 semantic cache can reuse it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import yaml

from finpilot.config import get_settings
from finpilot.llm.client import LLMClient, LLMError
from finpilot.llm.schemas import IntentResponse
from finpilot.redis_client import get_redis

log = logging.getLogger(__name__)
PERIODS = ("this_month", "last_month", "30d", "3m", "6m", "12m")


@dataclass(frozen=True)
class IntentSpec:
    name: str
    handler: str
    cacheable: bool
    ttl: int
    time_relative: bool
    patterns: tuple[re.Pattern[str], ...]
    examples: tuple[str, ...]


@dataclass
class Route:
    intent: str
    period: str
    via: str  # chip | rules | embedding | llm | default
    confidence: float
    spec: IntentSpec
    vector: list[float] | None = field(default=None, repr=False)


@lru_cache
def config() -> dict[str, Any]:
    raw = yaml.safe_load((get_settings().config_dir / "intents.yml").read_text(encoding="utf-8"))
    raw["_specs"] = {
        name: IntentSpec(
            name=name,
            handler=v["handler"],
            cacheable=bool(v["cacheable"]),
            ttl=int(v["ttl"]),
            time_relative=bool(v["time_relative"]),
            patterns=tuple(re.compile(p, re.I) for p in v.get("patterns") or []),
            examples=tuple(v.get("examples") or []),
        )
        for name, v in raw["intents"].items()
    }
    raw["_periods"] = [(re.compile(p["pattern"], re.I), p["period"]) for p in raw["periods"]]
    return raw


def spec(intent: str) -> IntentSpec:
    specs = config()["_specs"]
    if intent not in specs:
        raise KeyError(intent)
    return specs[intent]


def intents() -> list[str]:
    return list(config()["_specs"])


def normalize(q: str) -> str:
    return " ".join(q.lower().replace("’", "'").split())


def period_of(q: str) -> str | None:
    for pat, period in config()["_periods"]:
        if pat.search(q):
            return period
    return None


ANSWER_HANDLERS = ("deterministic_template", "llm_from_facts")


def answerable() -> list[str]:
    """Intents with a canonical answer (chips, prewarm, pipeline answer_from_cache)."""
    return [n for n, sp in config()["_specs"].items() if sp.handler in ANSWER_HANDLERS]


def content_words(t: str) -> set[str]:
    """Lower-case words of 4+ letters, singular-ish (trailing s dropped)."""
    return {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", t.lower())}


def canonical_vocabulary() -> set[str]:
    """Every word the canonical (answerable) questions use, plus question and generic words."""
    st = config()["settings"]
    words = " ".join([*st["generic_entity_words"], *st["question_words"]])
    for name in answerable():
        sp = config()["_specs"][name]
        pats = (re.sub(r"\\[a-zA-Z]", " ", p.pattern) for p in sp.patterns)  # drop \b, \s…
        words += " " + " ".join([*sp.examples, *pats])
    return content_words(words)


def specific_words(question: str) -> set[str]:
    """Content words of the question that no canonical question uses ("cricket", "gear")."""
    return content_words(question) - canonical_vocabulary()


def by_rules(q: str) -> str | None:
    for name, sp in config()["_specs"].items():
        if any(p.search(q) for p in sp.patterns):
            return name
    return None


def _example_vectors(llm: LLMClient) -> dict[str, np.ndarray]:
    """{intent: matrix of example vectors}, cached in Redis per embedding model + examples."""
    specs = {n: s for n, s in config()["_specs"].items() if s.examples}
    blob = json.dumps({n: s.examples for n, s in specs.items()}, sort_keys=True)
    key = f"cache:emb:intents:{get_settings().mesh_model_embed}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"
    r = get_redis()
    try:
        raw = r.get(key)
    except Exception:
        raw = None
    if raw:
        data = json.loads(raw)
    else:
        names = [n for n, s in specs.items() for _ in s.examples]
        vecs = llm.embed([e for s in specs.values() for e in s.examples], agent="intent_router")
        data = {}
        for n, v in zip(names, vecs, strict=True):
            data.setdefault(n, []).append(v)
        try:
            r.set(key, json.dumps(data), ex=30 * 86400)
        except Exception:
            log.debug("could not cache intent example vectors", exc_info=True)
    return {n: np.array(v, dtype=float) for n, v in data.items()}


def _unit(v: Any) -> np.ndarray:
    a = np.array(v, dtype=float)
    n = float(np.linalg.norm(a))
    return a / n if n else a


def route(question: str, llm: LLMClient | None = None, user_id: object | None = None) -> Route:
    q = normalize(question)
    period = period_of(q) or config()["settings"]["default_period"]
    hit = by_rules(q)
    if hit:
        return Route(hit, period, "rules", 1.0, spec(hit))
    vector: list[float] | None = None
    if llm is not None and llm.available:
        try:
            if llm.embeddings_available():
                vector = llm.embed([question], agent="intent_router", user_id=user_id)[0]
                qv = _unit(vector)
                best, score = None, -1.0
                for name, mat in _example_vectors(llm).items():
                    sims = (mat / np.linalg.norm(mat, axis=1, keepdims=True)) @ qv
                    if float(sims.max()) > score:
                        best, score = name, float(sims.max())
                if best and score >= float(config()["settings"]["embedding_threshold"]):
                    return Route(best, period, "embedding", round(score, 4), spec(best), vector)
        except LLMError:
            log.info("intent router: embeddings unavailable", exc_info=True)
        try:
            listing = "\n".join(
                f"- {n}: {'; '.join(s.examples) or 'anything else'}"
                for n, s in config()["_specs"].items()
            )
            out, _ = llm.chat_json(
                "intent_router/classify",
                {"intents": listing, "question": question[:500]},
                IntentResponse,
                agent="intent_router",
                user_id=user_id,
                cache_scope=str(user_id) if user_id else "global",
            )
            if out.intent in config()["_specs"] and out.confidence >= 0.5:
                p = out.period if out.period in PERIODS else period
                return Route(out.intent, p, "llm", out.confidence, spec(out.intent), vector)
        except LLMError:
            log.info("intent router: LLM classification unavailable", exc_info=True)
    return Route("free_form", period, "default", 0.0, spec("free_form"), vector)
