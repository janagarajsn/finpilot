"""Category management. Every function takes the tenant `user_id` and filters by it
(RLS enforces the same underneath). System categories are read-only but can be hidden.

Analyzer/forecaster/simulator rely on `flow_type` + `nature`, never on names, so custom
categories work everywhere.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.db.session import rowcount

KEYWORD_CONFIDENCE = 0.9
_COLS = """c.id, c.user_id, c.slug, c.name, c.flow_type, c.nature, c.color, c.icon, c.description,
  c.keywords_json AS keywords, c.examples_json AS examples, c.is_system, c.archived"""


class CategoryError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_keywords(keywords: list[str] | None) -> list[str]:
    out: list[str] = []
    for k in keywords or []:
        k = re.sub(r"\s+", " ", str(k)).strip()
        if 2 <= len(k) <= 40 and k.lower() not in {x.lower() for x in out}:
            out.append(k)
    if len(out) > 25:
        raise CategoryError("At most 25 keywords per category.", 422)
    return out


# ---------------------------------------------------------------------------- reads
def list_categories(
    s: Session, user_id: uuid.UUID, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    rows = s.execute(
        text(f"""
        SELECT {_COLS},
               (h.category_id IS NOT NULL) AS hidden,
               (SELECT count(*) FROM transactions t WHERE t.user_id = :u AND t.category_id = c.id) AS txn_count
        FROM categories c
        LEFT JOIN hidden_categories h ON h.category_id = c.id AND h.user_id = :u
        WHERE (c.user_id IS NULL OR c.user_id = :u) AND (:arch OR NOT c.archived)
        ORDER BY CASE c.flow_type WHEN 'income' THEN 0 WHEN 'expense' THEN 1 WHEN 'savings' THEN 2 ELSE 3 END,
                 c.is_system DESC, lower(c.name)
        """),  # noqa: S608 — constant column list
        {"u": user_id, "arch": include_archived},
    ).mappings()
    return [dict(r) for r in rows]


def get_category(s: Session, user_id: uuid.UUID, category_id: uuid.UUID) -> dict[str, Any] | None:
    sql = f"SELECT {_COLS} FROM categories c WHERE c.id = :id AND (c.user_id IS NULL OR c.user_id = :u)"  # noqa: S608 — constant
    row = s.execute(text(sql), {"id": category_id, "u": user_id}).mappings().first()
    return dict(row) if row else None


def require_visible(
    s: Session, user_id: uuid.UUID, category_id: uuid.UUID | None
) -> dict[str, Any] | None:
    if category_id is None:
        return None
    cat = get_category(s, user_id, category_id)
    if cat is None:
        raise CategoryError("Category not found.", 404)
    return cat


def _require_own(s: Session, user_id: uuid.UUID, category_id: uuid.UUID) -> dict[str, Any]:
    cat = require_visible(s, user_id, category_id)
    assert cat is not None
    if cat["is_system"]:
        raise CategoryError("System categories are read-only. You can hide them instead.", 403)
    return cat


def name_index(s: Session, user_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """lower(name) → id for every category usable by this user (imports, templates)."""
    rows = s.execute(
        text(
            "SELECT id, name FROM categories WHERE (user_id IS NULL OR user_id = :u) AND NOT archived"
        ),
        {"u": user_id},
    )
    return {r[1].strip().lower(): r[0] for r in rows}


# ---------------------------------------------------------------------------- writes
def _check_name_free(
    s: Session, user_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None
) -> None:
    clash = s.execute(
        text(
            "SELECT 1 FROM categories WHERE (user_id IS NULL OR user_id = :u) AND lower(btrim(name)) = lower(btrim(:n)) "
            "AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))"
        ),
        {"u": user_id, "n": name, "x": exclude},
    ).first()
    if clash:
        raise CategoryError(f"A category named “{name.strip()}” already exists.", 409)


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("flow_type") and data["flow_type"] != "expense":
        data["nature"] = "n/a"
    elif data.get("flow_type") == "expense" and data.get("nature") in (None, "n/a"):
        data["nature"] = "discretionary"
    if "keywords" in data:
        data["keywords"] = clean_keywords(data["keywords"])
    if "name" in data and data["name"] is not None:
        data["name"] = re.sub(r"\s+", " ", data["name"]).strip()
        if not 1 <= len(data["name"]) <= 60:
            raise CategoryError("Name must be 1–60 characters.", 422)
    return data


def create_category(s: Session, user_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    data = _validate(dict(data))
    _check_name_free(s, user_id, data["name"])
    cid = s.execute(
        text("""INSERT INTO categories (user_id, name, flow_type, nature, color, icon, description, keywords_json)
                VALUES (:u, :name, :flow_type, :nature, :color, :icon, :description, CAST(:kw AS jsonb))
                RETURNING id"""),
        {
            "u": user_id,
            "name": data["name"],
            "flow_type": data["flow_type"],
            "nature": data["nature"],
            "color": data.get("color") or "#6366f1",
            "icon": data.get("icon"),
            "description": (data.get("description") or "").strip(),
            "kw": json.dumps(data.get("keywords", [])),
        },
    ).scalar_one()
    out = get_category(s, user_id, cid)
    assert out is not None
    return out


def update_category(
    s: Session, user_id: uuid.UUID, category_id: uuid.UUID, changes: dict[str, Any]
) -> dict[str, Any]:
    cat = _require_own(s, user_id, category_id)
    changes = _validate({k: v for k, v in changes.items() if v is not None})
    if "flow_type" in changes and "nature" not in changes and changes["flow_type"] == "expense":
        changes["nature"] = cat["nature"] if cat["nature"] != "n/a" else "discretionary"
    if "name" in changes:
        _check_name_free(s, user_id, changes["name"], exclude=category_id)
    allowed = {"name", "flow_type", "nature", "color", "icon", "description", "archived"}
    sets = {k: v for k, v in changes.items() if k in allowed}
    if "keywords" in changes:
        sets["keywords_json"] = json.dumps(changes["keywords"])
    if sets:
        assign = ", ".join(
            f"{k} = CAST(:{k} AS jsonb)" if k == "keywords_json" else f"{k} = :{k}" for k in sets
        )
        s.execute(
            text(f"UPDATE categories SET {assign} WHERE id = :id AND user_id = :u"),  # noqa: S608 — allowlisted
            {**sets, "id": category_id, "u": user_id},
        )
    out = get_category(s, user_id, category_id)
    assert out is not None
    return out


def set_hidden(s: Session, user_id: uuid.UUID, category_id: uuid.UUID, hidden: bool) -> None:
    require_visible(s, user_id, category_id)
    if hidden:
        s.execute(
            text(
                "INSERT INTO hidden_categories (user_id, category_id) VALUES (:u, :c) ON CONFLICT DO NOTHING"
            ),
            {"u": user_id, "c": category_id},
        )
    else:
        s.execute(
            text("DELETE FROM hidden_categories WHERE user_id = :u AND category_id = :c"),
            {"u": user_id, "c": category_id},
        )


def _reassign(s: Session, user_id: uuid.UUID, src: uuid.UUID, dst: uuid.UUID) -> int:
    moved = rowcount(
        s.execute(
            text(
                "UPDATE transactions SET category_id = :d WHERE user_id = :u AND category_id = :s"
            ),
            {"u": user_id, "s": src, "d": dst},
        )
    )
    s.execute(
        text("UPDATE recurring_rules SET category_id = :d WHERE user_id = :u AND category_id = :s"),
        {"u": user_id, "s": src, "d": dst},
    )
    s.execute(
        text("UPDATE merchant_memory SET category_id = :d WHERE user_id = :u AND category_id = :s"),
        {"u": user_id, "s": src, "d": dst},
    )
    return int(moved or 0)


def delete_category(
    s: Session, user_id: uuid.UUID, category_id: uuid.UUID, reassign_to: uuid.UUID | None = None
) -> int:
    _require_own(s, user_id, category_id)
    in_use = s.execute(
        text("SELECT count(*) FROM transactions WHERE user_id = :u AND category_id = :c"),
        {"u": user_id, "c": category_id},
    ).scalar_one()
    moved = 0
    if in_use:
        if reassign_to is None:
            raise CategoryError(
                f"{in_use} transactions use this category. Choose a category to move them to.", 409
            )
        if reassign_to == category_id:
            raise CategoryError("Choose a different category.", 422)
        require_visible(s, user_id, reassign_to)
        moved = _reassign(s, user_id, category_id, reassign_to)
    elif reassign_to is not None:
        require_visible(s, user_id, reassign_to)
        _reassign(s, user_id, category_id, reassign_to)
    s.execute(
        text("DELETE FROM categories WHERE id = :c AND user_id = :u"),
        {"c": category_id, "u": user_id},
    )
    return moved


def merge_categories(
    s: Session, user_id: uuid.UUID, source_id: uuid.UUID, into_id: uuid.UUID
) -> int:
    if source_id == into_id:
        raise CategoryError("Cannot merge a category into itself.", 422)
    _require_own(s, user_id, source_id)
    require_visible(s, user_id, into_id)
    moved = _reassign(s, user_id, source_id, into_id)
    s.execute(
        text("DELETE FROM categories WHERE id = :c AND user_id = :u"),
        {"c": source_id, "u": user_id},
    )
    return moved


# ---------------------------------------------------------------------------- deterministic matching
def suggest(
    s: Session, user_id: uuid.UUID, query: str, merchant_norm: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Instant suggestions while typing (no LLM): merchant memory → user keywords → system examples."""
    q = (query or "").strip()
    if len(q) < 2 and not merchant_norm:
        return []
    out: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()

    def add(cat_id: uuid.UUID, source: str, confidence: float) -> None:
        if cat_id in seen or len(out) >= limit:
            return
        cat = get_category(s, user_id, cat_id)
        if cat and not cat["archived"]:
            seen.add(cat_id)
            out.append(
                {
                    "category_id": cat_id,
                    "name": cat["name"],
                    "color": cat["color"],
                    "icon": cat["icon"],
                    "source": source,
                    "confidence": confidence,
                }
            )

    if merchant_norm:
        mm = s.execute(
            text(
                "SELECT category_id FROM merchant_memory WHERE user_id = :u AND merchant_norm = :m"
            ),
            {"u": user_id, "m": merchant_norm},
        ).first()
        if mm:
            add(mm[0], "memory", 0.95)
    for cid in keyword_matches(s, user_id, q):
        add(cid, "rule", KEYWORD_CONFIDENCE)
    if q:
        rows = s.execute(
            text("""
            SELECT c.id FROM categories c, jsonb_array_elements_text(c.examples_json || c.keywords_json) ex
            WHERE (c.user_id IS NULL OR c.user_id = :u) AND NOT c.archived
              AND (upper(:q) LIKE '%' || upper(ex) || '%' OR upper(ex) LIKE '%' || upper(:q) || '%')
            GROUP BY c.id ORDER BY max(length(ex)) DESC LIMIT 5"""),
            {"u": user_id, "q": q},
        )
        for r in rows:
            add(r[0], "example", 0.5)
    return out


def keyword_matches(s: Session, user_id: uuid.UUID, description: str) -> list[uuid.UUID]:
    """User keyword rules (confidence 0.9): case-insensitive whole-word-ish containment."""
    if not description:
        return []
    rows = s.execute(
        text("""
        SELECT c.id, kw FROM categories c, jsonb_array_elements_text(c.keywords_json) kw
        WHERE c.user_id = :u AND NOT c.archived"""),
        {"u": user_id},
    )
    hay = f" {re.sub(r'[^A-Z0-9]+', ' ', description.upper())} "
    hits: list[tuple[int, uuid.UUID]] = []
    for cid, kw in rows:
        needle = re.sub(r"[^A-Z0-9]+", " ", kw.upper()).strip()
        if needle and f" {needle} " in hay:
            hits.append((len(needle), cid))
    return [cid for _, cid in sorted(hits, key=lambda x: -x[0])]


class CategoryResolver:
    """Preloaded merchant memory + keyword rules (+ the system rule lexicon when the amount sign
    is known) for instant, per-row categorization without per-row queries."""

    def __init__(self, s: Session, user_id: uuid.UUID) -> None:
        self.memory = {
            m: c
            for m, c in s.execute(
                text("SELECT merchant_norm, category_id FROM merchant_memory WHERE user_id = :u"),
                {"u": user_id},
            )
        }
        kws = [
            (re.sub(r"[^A-Z0-9]+", " ", kw.upper()).strip(), cid)
            for cid, kw in s.execute(
                text("""SELECT c.id, kw FROM categories c, jsonb_array_elements_text(c.keywords_json) kw
                                            WHERE c.user_id = :u AND NOT c.archived"""),
                {"u": user_id},
            )
        ]
        self.keywords = sorted([k for k in kws if k[0]], key=lambda k: -len(k[0]))
        rows = s.execute(
            text("""SELECT c.id, c.slug, c.flow_type FROM categories c
                                 LEFT JOIN hidden_categories h ON h.category_id = c.id AND h.user_id = :u
                                 WHERE (c.user_id IS NULL OR c.user_id = :u) AND NOT c.archived
                                   AND h.category_id IS NULL"""),
            {"u": user_id},
        ).all()
        self.slug_to_id = {r.slug: r.id for r in rows if r.slug}
        self.flow = {r.id: r.flow_type for r in rows}

    def resolve(
        self, description: str, merchant: str | None, amount_sign: int = 0
    ) -> tuple[uuid.UUID, str, float, str] | None:
        from finpilot.categories import rules as lexicon

        def flow_ok(cid: uuid.UUID) -> bool:
            f = self.flow.get(cid)
            if f is None:
                return False
            return amount_sign == 0 or not (
                (f == "income" and amount_sign < 0) or (f == "expense" and amount_sign > 0)
            )

        if merchant and merchant in self.memory and flow_ok(self.memory[merchant]):
            return self.memory[merchant], "memory", 0.95, f"Merchant memory for {merchant}"
        hay = f" {re.sub(r'[^A-Z0-9]+', ' ', description.upper())} "
        for needle, cid in self.keywords:
            if f" {needle} " in hay and flow_ok(cid):
                return cid, "rule", KEYWORD_CONFIDENCE, "Matched one of your category keywords"
        if amount_sign:
            hit = lexicon.match(merchant or "", description, amount_sign)
            accept = float(lexicon.thresholds()["categorizer"]["rules_accept_threshold"])
            if hit and hit.confidence >= accept and hit.category_slug in self.slug_to_id:
                cid = self.slug_to_id[hit.category_slug]
                if flow_ok(cid):
                    return cid, "rule", hit.confidence, f"Rule {hit.pattern}"
        return None


@dataclass(frozen=True)
class RecatCandidate:
    transaction_id: uuid.UUID
    date: str
    description: str
    amount: str
    currency: str
    current_category: str | None
    matched_keyword: str


def recategorize_preview(
    s: Session, user_id: uuid.UUID, category_id: uuid.UUID, limit: int = 500
) -> list[RecatCandidate]:
    """Existing NON-user-set transactions that the category's keywords would now claim."""
    cat = require_visible(s, user_id, category_id)
    assert cat is not None
    kws = cat["keywords"] or []
    if not kws:
        return []
    rows = s.execute(
        text("""
        SELECT t.id, t.date, t.raw_description, t.amount, t.currency, oc.name AS current_name, t.merchant_norm
        FROM transactions t LEFT JOIN categories oc ON oc.id = t.category_id
        WHERE t.user_id = :u AND t.cat_source IS DISTINCT FROM 'user'
          AND t.category_id IS DISTINCT FROM :c
          AND EXISTS (SELECT 1 FROM unnest(CAST(:pats AS text[])) p WHERE upper(t.raw_description) LIKE p ESCAPE '\\')
        ORDER BY t.date DESC LIMIT :lim"""),
        {
            "u": user_id,
            "c": category_id,
            "lim": limit,
            "pats": [f"%{_like_escape(k.upper())}%" for k in kws],
        },
    )
    out = []
    for r in rows:
        desc = r.raw_description.upper()
        kw = next((k for k in kws if k.upper() in desc), kws[0])
        out.append(
            RecatCandidate(
                r.id,
                r.date.isoformat(),
                r.raw_description,
                str(r.amount),
                r.currency,
                r.current_name,
                kw,
            )
        )
    return out


def recategorize_apply(
    s: Session, user_id: uuid.UUID, category_id: uuid.UUID, transaction_ids: list[uuid.UUID]
) -> int:
    """Apply a previewed diff. Re-validates: only ids still eligible are changed."""
    eligible = {
        c.transaction_id: c for c in recategorize_preview(s, user_id, category_id, limit=50_000)
    }
    chosen = [t for t in transaction_ids if t in eligible]
    if not chosen:
        return 0
    cat = get_category(s, user_id, category_id)
    assert cat is not None
    for tid in chosen:
        s.execute(
            text("""UPDATE transactions SET category_id = :c, cat_source = 'rule', cat_confidence = :conf,
                          cat_rationale = :why, needs_review = false WHERE id = :t AND user_id = :u"""),
            {
                "c": category_id,
                "conf": KEYWORD_CONFIDENCE,
                "t": tid,
                "u": user_id,
                "why": f"Keyword “{eligible[tid].matched_keyword}” of category {cat['name']}",
            },
        )
    return len(chosen)
