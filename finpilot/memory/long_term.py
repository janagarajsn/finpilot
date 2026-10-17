"""Long-term semantic memory: PostgreSQL + pgvector behind a `SemanticMemory` protocol.

Why Postgres and not a vector DB: one fewer service, writes are transactional with the user's
data, RLS + a user_id filter make per-user search trivial at this scale, and backup/delete-by-user
come for free (DESIGN_NOTES §11). A Qdrant adapter would implement the same protocol.

Write rules (deterministic, after extraction):
- blocked content (account/card numbers, credentials, codes) is never stored
- same kind + topic as an active memory: near-identical → merge (update); different → supersede
  (the old row points to the new one and stops being recalled)
- otherwise an active memory with similarity > 0.9 → merge; else insert
- `expires_at` in the past = not recalled; superseded rows are kept (history) until deleted

Without embeddings (Mesh down or EMBEDDINGS_ENABLED=false) rows are stored with a NULL vector and
matched lexically (English full-text rank; trigram similarity for dedupe).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.db.session import rowcount
from finpilot.llm.client import LLMClient, LLMError
from finpilot.memory.config import blocked, config

log = logging.getLogger(__name__)
Kind = Literal["fact", "preference", "goal_context", "decision", "episode"]
KINDS = ("fact", "preference", "goal_context", "decision", "episode")
ACTIVE = "superseded_by IS NULL AND (expires_at IS NULL OR expires_at > now())"
COLUMNS = (
    "id, kind, topic, text, importance, confidence, pinned, source, source_ref, created_at, "
    "updated_at, last_used_at, expires_at, superseded_by"
)


@dataclass
class MemoryIn:
    kind: str
    topic: str
    text: str
    importance: float = 0.5
    confidence: float = 0.8
    source: str = "chat"
    source_ref: str | None = None
    expires_at: dt.datetime | None = None
    pinned: bool = False


@dataclass
class WriteOutcome:
    action: Literal["added", "merged", "superseded", "blocked"]
    id: uuid.UUID | None = None
    previous_id: uuid.UUID | None = None


@dataclass
class Scored:
    row: dict[str, Any]
    similarity: float
    score: float = 0.0
    via: str = "lexical"  # vector | lexical | pinned


@dataclass
class _Ctx:
    s: Session
    uid: uuid.UUID
    llm: LLMClient | None
    vectors: dict[str, list[float] | None] = field(default_factory=dict)


class SemanticMemory(Protocol):
    def write(self, s: Session, uid: uuid.UUID, m: MemoryIn) -> WriteOutcome: ...
    def search(self, s: Session, uid: uuid.UUID, query: str) -> list[Scored]: ...
    def list_all(
        self, s: Session, uid: uuid.UUID, include_inactive: bool = False
    ) -> list[dict[str, Any]]: ...
    def update(
        self,
        s: Session,
        uid: uuid.UUID,
        mid: uuid.UUID,
        *,
        text_: str | None = None,
        pinned: bool | None = None,
        importance: float | None = None,
    ) -> dict[str, Any] | None: ...
    def delete(self, s: Session, uid: uuid.UUID, mid: uuid.UUID) -> bool: ...
    def wipe(self, s: Session, uid: uuid.UUID) -> int: ...


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def _tsquery(q: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 1]
    return " | ".join(dict.fromkeys(words))


def _norm(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", t.lower()))


class PgMemory(SemanticMemory):
    """pgvector implementation. `llm` supplies embeddings via Mesh (None → lexical only)."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

    # ------------------------------------------------------------------ embeddings
    def embed(self, s: Session, uid: uuid.UUID, texts_: list[str]) -> list[list[float]] | None:
        if self.llm is None or not self.llm.available:
            return None
        try:
            if not self.llm.embeddings_available():
                return None
            return self.llm.embed(texts_, agent="memory", user_id=uid)
        except LLMError:
            log.info("memory: embeddings unavailable, lexical fallback", exc_info=True)
            return None

    # ------------------------------------------------------------------ write
    def write(
        self, s: Session, uid: uuid.UUID, m: MemoryIn, vector: list[float] | None = None
    ) -> WriteOutcome:
        txt = " ".join(m.text.split())[:500]
        if not txt or blocked(txt):
            return WriteOutcome("blocked")
        if m.kind not in KINDS:
            raise ValueError(f"unknown memory kind {m.kind!r}")
        topic = (_norm(m.topic).replace(" ", "_") or "general")[:60]
        if vector is None:
            got = self.embed(s, uid, [txt])
            vector = got[0] if got else None
        cfg = config()["long_term"]
        same_slot = s.execute(
            text(f"""SELECT id, text, pinned,
                            CASE WHEN CAST(:v AS vector) IS NOT NULL AND embedding IS NOT NULL
                                 THEN 1 - (embedding <=> CAST(:v AS vector)) END AS cos,
                            similarity(text, :t) AS trgm
                     FROM memories WHERE user_id = :u AND kind = :k AND topic = :topic AND {ACTIVE}
                     ORDER BY updated_at DESC LIMIT 1"""),  # noqa: S608 — constant fragments, bound params
            {
                "u": uid,
                "k": m.kind,
                "topic": topic,
                "t": txt,
                "v": _vec(vector) if vector else None,
            },
        ).first()
        if same_slot is not None:
            if self._near(same_slot.cos, same_slot.trgm, same_slot.text, txt, cfg):
                self._merge(s, uid, same_slot.id, m, txt, vector)
                return WriteOutcome("merged", same_slot.id)
            new_id = self._insert(
                s, uid, m, topic, txt, vector, pinned=m.pinned or same_slot.pinned
            )
            s.execute(
                text("UPDATE memories SET superseded_by = :n WHERE id = :o AND user_id = :u"),
                {"n": new_id, "o": same_slot.id, "u": uid},
            )
            return WriteOutcome("superseded", new_id, same_slot.id)
        close = s.execute(
            text(f"""SELECT id, text,
                            CASE WHEN CAST(:v AS vector) IS NOT NULL AND embedding IS NOT NULL
                                 THEN 1 - (embedding <=> CAST(:v AS vector)) END AS cos,
                            similarity(text, :t) AS trgm
                     FROM memories WHERE user_id = :u AND {ACTIVE}
                     ORDER BY greatest(coalesce(1 - (embedding <=> CAST(:v AS vector)), 0), similarity(text, :t)) DESC
                     LIMIT 1"""),  # noqa: S608 — constant fragments, bound params
            {"u": uid, "t": txt, "v": _vec(vector) if vector else None},
        ).first()
        if close is not None and self._near(close.cos, close.trgm, close.text, txt, cfg):
            self._merge(s, uid, close.id, m, txt, vector)
            return WriteOutcome("merged", close.id)
        return WriteOutcome("added", self._insert(s, uid, m, topic, txt, vector, pinned=m.pinned))

    @staticmethod
    def _near(cos: Any, trgm: Any, old: str, new: str, cfg: dict[str, Any]) -> bool:
        if _norm(old) == _norm(new):
            return True
        if cos is not None:
            return float(cos) > float(cfg["dedupe_similarity"])
        return float(trgm or 0) >= float(cfg["lexical_dedupe_similarity"])

    def _insert(
        self,
        s: Session,
        uid: uuid.UUID,
        m: MemoryIn,
        topic: str,
        txt: str,
        vector: list[float] | None,
        pinned: bool,
    ) -> uuid.UUID:
        return uuid.UUID(
            str(
                s.execute(
                    text("""INSERT INTO memories (user_id, kind, topic, text, embedding, importance, confidence,
                                                  pinned, source, source_ref, expires_at)
                            VALUES (:u, :k, :topic, :t, CAST(:v AS vector), :imp, :conf, :pin, :src, :ref, :exp)
                            RETURNING id"""),
                    {
                        "u": uid,
                        "k": m.kind,
                        "topic": topic,
                        "t": txt,
                        "v": _vec(vector) if vector else None,
                        "imp": round(min(max(m.importance, 0.0), 1.0), 2),
                        "conf": round(min(max(m.confidence, 0.0), 1.0), 2),
                        "pin": pinned,
                        "src": m.source,
                        "ref": m.source_ref,
                        "exp": m.expires_at,
                    },
                ).scalar_one()
            )
        )

    def _merge(
        self,
        s: Session,
        uid: uuid.UUID,
        mid: uuid.UUID,
        m: MemoryIn,
        txt: str,
        vector: list[float] | None,
    ) -> None:
        """The newer wording wins; importance/confidence never drop; a new expiry replaces the old."""
        s.execute(
            text("""UPDATE memories SET text = :t, embedding = coalesce(CAST(:v AS vector), embedding),
                           importance = greatest(importance, :imp), confidence = greatest(confidence, :conf),
                           expires_at = :exp, source_ref = coalesce(:ref, source_ref)
                    WHERE id = :id AND user_id = :u"""),
            {
                "t": txt,
                "v": _vec(vector) if vector else None,
                "imp": round(min(max(m.importance, 0.0), 1.0), 2),
                "conf": round(min(max(m.confidence, 0.0), 1.0), 2),
                "exp": m.expires_at,
                "ref": m.source_ref,
                "id": mid,
                "u": uid,
            },
        )

    # ------------------------------------------------------------------ read
    def candidates(
        self, s: Session, uid: uuid.UUID, query: str, vector: list[float] | None
    ) -> list[Scored]:
        """Every active memory with its similarity to `query` (vector cosine where both sides
        have a vector, else lexical rank). Users hold hundreds of memories, not millions, so the
        scoring is a scan; the HNSW index serves larger stores."""
        tsq = _tsquery(query)
        rows = s.execute(
            text(f"""SELECT {COLUMNS},
                            CASE WHEN CAST(:v AS vector) IS NOT NULL AND embedding IS NOT NULL
                                 THEN 1 - (embedding <=> CAST(:v AS vector)) END AS cos,
                            CASE WHEN :q <> '' THEN ts_rank_cd(tsv, to_tsquery('english', :q), 32) ELSE 0 END AS lex
                     FROM memories WHERE user_id = :u AND {ACTIVE}
                     ORDER BY updated_at DESC LIMIT 1000"""),  # noqa: S608 — constant fragments, bound params
            {"u": uid, "v": _vec(vector) if vector else None, "q": tsq},
        ).mappings()
        out = []
        for r in rows:
            d = dict(r)
            cos, lex = d.pop("cos"), float(d.pop("lex") or 0)
            out.append(
                Scored(d, float(cos), via="vector")
                if cos is not None
                else Scored(d, lex, via="lexical")
            )
        return out

    def search(self, s: Session, uid: uuid.UUID, query: str) -> list[Scored]:
        from finpilot.memory.retrieval import rank

        got = self.embed(s, uid, [query[:500]])
        return rank(self.candidates(s, uid, query, got[0] if got else None))

    def list_all(
        self, s: Session, uid: uuid.UUID, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        where = "TRUE" if include_inactive else ACTIVE
        return [
            dict(r)
            for r in s.execute(
                text(f"""SELECT {COLUMNS}, ({ACTIVE}) AS active FROM memories
                         WHERE user_id = :u AND {where}
                         ORDER BY pinned DESC, ({ACTIVE}) DESC, updated_at DESC"""),  # noqa: S608 — constant fragments, bound params
                {"u": uid},
            ).mappings()
        ]

    def get(self, s: Session, uid: uuid.UUID, mid: uuid.UUID) -> dict[str, Any] | None:
        r = (
            s.execute(
                text(
                    f"SELECT {COLUMNS}, ({ACTIVE}) AS active FROM memories WHERE id = :i AND user_id = :u"  # noqa: S608
                ),
                {"i": mid, "u": uid},
            )
            .mappings()
            .first()
        )
        return dict(r) if r else None

    def touch(self, s: Session, uid: uuid.UUID, ids: list[uuid.UUID]) -> None:
        if ids:
            s.execute(
                text(
                    "UPDATE memories SET last_used_at = now() WHERE user_id = :u AND id = ANY(:ids)"
                ),
                {"u": uid, "ids": ids},
            )

    # ------------------------------------------------------------------ user edits
    def update(
        self,
        s: Session,
        uid: uuid.UUID,
        mid: uuid.UUID,
        *,
        text_: str | None = None,
        pinned: bool | None = None,
        importance: float | None = None,
    ) -> dict[str, Any] | None:
        cur = self.get(s, uid, mid)
        if cur is None:
            return None
        if text_ is not None:
            t = " ".join(text_.split())[:500]
            if not t or blocked(t):
                raise ValueError("memory text is empty or contains blocked content")
            got = self.embed(s, uid, [t])
            s.execute(
                text("""UPDATE memories SET text = :t, embedding = CAST(:v AS vector), source = 'user'
                        WHERE id = :i AND user_id = :u"""),
                {"t": t, "v": _vec(got[0]) if got else None, "i": mid, "u": uid},
            )
        if pinned is not None:
            s.execute(
                text("UPDATE memories SET pinned = :p WHERE id = :i AND user_id = :u"),
                {"p": pinned, "i": mid, "u": uid},
            )
        if importance is not None:
            s.execute(
                text("UPDATE memories SET importance = :imp WHERE id = :i AND user_id = :u"),
                {"imp": round(min(max(importance, 0.0), 1.0), 2), "i": mid, "u": uid},
            )
        return self.get(s, uid, mid)

    def delete(self, s: Session, uid: uuid.UUID, mid: uuid.UUID) -> bool:
        """Removes the row, its vector and the older versions it superseded (otherwise
        `ON DELETE SET NULL` would make a contradicted memory active again)."""
        n = rowcount(
            s.execute(
                text("""WITH RECURSIVE chain AS (
                      SELECT id FROM memories WHERE id = :i AND user_id = :u
                      UNION SELECT m.id FROM memories m JOIN chain c ON m.superseded_by = c.id
                      WHERE m.user_id = :u)
                    DELETE FROM memories WHERE user_id = :u AND id IN (SELECT id FROM chain)"""),
                {"i": mid, "u": uid},
            )
        )
        return bool(n)

    def wipe(self, s: Session, uid: uuid.UUID) -> int:
        return int(rowcount(s.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": uid})))


def store(llm: LLMClient | None) -> PgMemory:
    return PgMemory(llm)
