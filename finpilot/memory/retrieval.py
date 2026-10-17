"""Memory retrieval: score = 0.6·similarity + 0.25·recency + 0.15·importance (config/memory.yml).

Pinned memories come first (the user said they always matter), then the best-scored matches
above the similarity floor, up to top-k and the token budget. The result is rendered by the
prompts themselves inside `<memory>` tags in their DYNAMIC section (untrusted data).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.orm import Session

from finpilot.llm.client import LLMClient
from finpilot.memory.config import config, tokens
from finpilot.memory.long_term import PgMemory, Scored


def recency(row: dict[str, Any], now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.UTC)
    ref = row.get("last_used_at") or row.get("updated_at") or row.get("created_at") or now
    age = max((now - ref).total_seconds() / 86400, 0.0)
    return float(0.5 ** (age / float(config()["retrieval"]["recency_half_life_days"])))


def rank(cands: list[Scored], k: int | None = None, budget: int | None = None) -> list[Scored]:
    cfg = config()["retrieval"]
    w = cfg["weights"]
    k = k or int(cfg["top_k"])
    budget = budget or int(cfg["token_budget"])
    for c in cands:
        c.score = (
            float(w["similarity"]) * c.similarity
            + float(w["recency"]) * recency(c.row)
            + float(w["importance"]) * float(c.row.get("importance") or 0)
        )

    def matches(c: Scored) -> bool:
        floor = cfg["min_similarity"] if c.via == "vector" else cfg["min_lexical"]
        return c.similarity >= float(floor)

    pinned = sorted((c for c in cands if c.row.get("pinned")), key=lambda c: -c.score)
    for c in pinned:
        c.via = "pinned" if not matches(c) else c.via
    rest = sorted(
        (c for c in cands if not c.row.get("pinned") and matches(c)), key=lambda c: -c.score
    )
    out: list[Scored] = []
    used = 0
    for c in [*pinned, *rest]:
        t = tokens(c.row["text"])
        if len(out) >= k or used + t > budget:
            break
        out.append(c)
        used += t
    return out


def recall(
    s: Session, uid: uuid.UUID, query: str, llm: LLMClient | None, k: int | None = None
) -> list[dict[str, Any]]:
    """Top memories for `query` as prompt-ready dicts; marks them used (recency)."""
    mem = PgMemory(llm)
    if k is None:
        hits = mem.search(s, uid, query)
    else:
        vec = mem.embed(s, uid, [query[:500]])
        hits = rank(mem.candidates(s, uid, query, vec[0] if vec else None), k=k)
    mem.touch(s, uid, [h.row["id"] for h in hits])
    return [
        {
            "id": str(h.row["id"]),
            "kind": h.row["kind"],
            "text": h.row["text"],
            "since": h.row["updated_at"].date().isoformat() if h.row.get("updated_at") else None,
            "score": round(h.score, 3),
            "via": h.via,
        }
        for h in hits
    ]
