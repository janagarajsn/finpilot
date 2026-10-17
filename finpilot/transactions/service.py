"""Transaction writes and reads (manual entry, edits, category corrections, FX overrides).

Amounts: callers pass a POSITIVE amount + kind (expense/income); storage is signed
(negative = expense) in the ORIGINAL currency, plus `fx_rate` (→ INR), `amount_inr`,
`fx_rate_date`, `fx_source`. Reads go through `v_transactions_reporting` for the user's
reporting currency. `users.data_version` bumps are done by Postgres triggers.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.db.session import rowcount
from finpilot.fx.service import FXService
from finpilot.ingest.normalizer import normalize_merchant
from finpilot.money import get_currency_registry, quantize

Kind = Literal["expense", "income"]
RATE_Q = Decimal("0.00000001")


class TxnError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class TxnInput:
    kind: Kind
    amount: Decimal  # positive
    date: dt.date
    description: str
    currency: str | None = None
    category_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    notes: str | None = None
    fx_rate: Decimal | None = None  # user override: INR per unit
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FxFields:
    fx_rate: Decimal
    amount_inr: Decimal
    fx_rate_date: dt.date
    fx_source: str
    stale: bool = False
    approximate: bool = False


# ---------------------------------------------------------------------------- helpers
def signed(kind: Kind, amount: Decimal) -> Decimal:
    if amount <= 0:
        raise TxnError("Amount must be greater than zero.", 422)
    return -amount if kind == "expense" else amount


def resolve_fx(
    fx: FXService | None,
    amount: Decimal,
    currency: str,
    date: dt.date,
    override: Decimal | None = None,
) -> FxFields:
    reg = get_currency_registry()
    if currency == reg.base:
        return FxFields(Decimal(1), quantize(amount, reg.base), date, "base")
    if override is not None:
        if override <= 0:
            raise TxnError("FX rate must be positive.", 422)
        rate = override.quantize(RATE_Q, ROUND_HALF_EVEN)
        return FxFields(rate, quantize(amount * rate, reg.base), date, "user_override")
    if fx is None:
        raise TxnError("FX service unavailable.", 503)
    r = fx.rate_to_inr(currency, date)
    return FxFields(
        r.rate_inr_per_unit,
        quantize(amount * r.rate_inr_per_unit, reg.base),
        r.rate_date,
        r.source,
        r.stale,
        r.approximate,
    )


def _squash(desc: str) -> str:
    return re.sub(r"\s+", " ", desc.strip().upper())


def dedupe_key(
    date: dt.date, amount: Decimal, currency: str, description: str, account_id: uuid.UUID | None
) -> str:
    raw = f"{date.isoformat()}|{amount}|{currency}|{_squash(description)}|{account_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _default_currency(s: Session, user_id: uuid.UUID, account_id: uuid.UUID | None) -> str:
    if account_id:
        cur = s.execute(
            text("SELECT currency FROM accounts WHERE id = :a AND user_id = :u"),
            {"a": account_id, "u": user_id},
        ).scalar()
        if cur is None:
            raise TxnError("Account not found.", 404)
        return str(cur)
    return str(
        s.execute(
            text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
    )


def _validate(s: Session, user_id: uuid.UUID, inp: TxnInput) -> tuple[str, str]:
    desc = re.sub(r"\s+", " ", (inp.description or "")).strip()
    if not 1 <= len(desc) <= 500:
        raise TxnError("Description is required (max 500 characters).", 422)
    if inp.notes and len(inp.notes) > 1000:
        raise TxnError("Notes are limited to 1000 characters.", 422)
    if inp.date > dt.date.today() + dt.timedelta(days=366):
        raise TxnError("Date is too far in the future.", 422)
    currency = (inp.currency or _default_currency(s, user_id, inp.account_id)).upper()
    if not get_currency_registry().is_supported(currency):
        raise TxnError(f"Unsupported currency {currency}.", 422)
    if inp.account_id:
        _default_currency(s, user_id, inp.account_id)  # ownership check
    if inp.category_id:
        try:
            categories.require_visible(s, user_id, inp.category_id)
        except categories.CategoryError as e:
            raise TxnError(str(e), e.status) from e
    return desc, currency


# ---------------------------------------------------------------------------- writes
_INSERT_SQL = text("""
INSERT INTO transactions (user_id, account_id, date, raw_description, merchant_norm, amount, currency,
  fx_rate, amount_inr, fx_rate_date, fx_source, category_id, cat_source, cat_confidence, cat_rationale,
  needs_review, notes, source, recurring_rule_id, import_batch_id, hash, dedupe_key)
VALUES (:user_id, :account_id, :date, :description, :merchant_norm, :amount, :currency,
  :fx_rate, :amount_inr, :fx_rate_date, :fx_source, :category_id, :cat_source, :cat_confidence, :cat_rationale,
  :needs_review, :notes, :source, :recurring_rule_id, :import_batch_id, :hash, :dedupe_key)
ON CONFLICT DO NOTHING
""")
_INSERT = text(_INSERT_SQL.text + "RETURNING id")
_INSERT_MANY = _INSERT_SQL


def build_row(
    s: Session,
    user_id: uuid.UUID,
    inp: TxnInput,
    fx: FXService | None,
    *,
    source: str = "manual",
    hash_suffix: str | None = None,
    cat_source: str | None = None,
    recurring_rule_id: uuid.UUID | None = None,
    import_batch_id: uuid.UUID | None = None,
    fx_fields: FxFields | None = None,
    resolver: categories.CategoryResolver | None = None,
) -> dict[str, Any]:
    desc, currency = _validate(s, user_id, inp)
    amount = quantize(signed(inp.kind, inp.amount), currency)
    f = fx_fields or resolve_fx(fx, amount, currency, inp.date, inp.fx_rate)
    if fx_fields is not None:  # pre-resolved rate (imports): recompute INR for this amount
        f = FxFields(
            f.fx_rate,
            quantize(amount * f.fx_rate, get_currency_registry().base),
            f.fx_rate_date,
            f.fx_source,
            f.stale,
            f.approximate,
        )
    key = dedupe_key(inp.date, amount, currency, desc, inp.account_id)
    merchant = normalize_merchant(desc)
    cat_id, conf, why = inp.category_id, None, None
    if cat_id:
        conf, cat_source = Decimal(1), cat_source or "user"
    else:
        # deterministic, instant: the user's merchant memory, then their keyword rules
        hit = (resolver or categories.CategoryResolver(s, user_id)).resolve(
            desc, merchant, 1 if amount > 0 else -1
        )
        if hit:
            cat_id, cat_source, conf_f, why = hit
            conf = Decimal(str(conf_f))
    return {
        "user_id": user_id,
        "account_id": inp.account_id,
        "date": inp.date,
        "description": desc,
        "merchant_norm": merchant or None,
        "amount": amount,
        "currency": currency,
        "fx_rate": f.fx_rate,
        "amount_inr": f.amount_inr,
        "fx_rate_date": f.fx_rate_date,
        "fx_source": f.fx_source,
        "category_id": cat_id,
        "cat_source": cat_source if cat_id else None,
        "cat_confidence": conf,
        "cat_rationale": why,
        "needs_review": False,
        "notes": (inp.notes or None),
        "source": source,
        "recurring_rule_id": recurring_rule_id,
        "import_batch_id": import_batch_id,
        "hash": f"{key}:{hash_suffix or 'm:' + uuid.uuid4().hex}",
        "dedupe_key": key,
    }


def create(
    s: Session, user_id: uuid.UUID, inp: TxnInput, fx: FXService | None, **kw: Any
) -> uuid.UUID | None:
    """Returns the new id, or None if an identical row (same hash) already exists."""
    row = build_row(s, user_id, inp, fx, **kw)
    return s.execute(_INSERT, row).scalar()


def insert_rows(s: Session, rows: list[dict[str, Any]]) -> int:
    """Bulk insert (executemany); duplicates by `hash` are skipped. Returns rows inserted."""
    if not rows:
        return 0
    count = text("SELECT count(*) FROM transactions WHERE user_id = :u")
    before = s.execute(count, {"u": rows[0]["user_id"]}).scalar_one()
    s.execute(_INSERT_MANY, rows)
    return int(s.execute(count, {"u": rows[0]["user_id"]}).scalar_one() - before)


def _require_txn(s: Session, user_id: uuid.UUID, txn_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("SELECT * FROM transactions WHERE id = :id AND user_id = :u"),
            {"id": txn_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TxnError("Transaction not found.", 404)
    return dict(row)


def update(
    s: Session, user_id: uuid.UUID, txn_id: uuid.UUID, changes: dict[str, Any], fx: FXService | None
) -> dict[str, Any]:
    cur = _require_txn(s, user_id, txn_id)
    kind: Kind = changes.get("kind") or ("expense" if cur["amount"] < 0 else "income")
    inp = TxnInput(
        kind=kind,
        amount=Decimal(str(changes["amount"]))
        if changes.get("amount") is not None
        else abs(cur["amount"]),
        date=changes.get("date") or cur["date"],
        description=changes.get("description") or cur["raw_description"],
        currency=(changes.get("currency") or cur["currency"]),
        category_id=cur["category_id"],
        account_id=changes["account_id"] if "account_id" in changes else cur["account_id"],
        notes=changes["notes"] if "notes" in changes else cur["notes"],
    )
    desc, currency = _validate(s, user_id, inp)
    amount = quantize(signed(inp.kind, inp.amount), currency)
    money_changed = (amount, currency, inp.date) != (cur["amount"], cur["currency"], cur["date"])
    params: dict[str, Any] = {
        "id": txn_id,
        "u": user_id,
        "date": inp.date,
        "description": desc,
        "merchant": normalize_merchant(desc),
        "amount": amount,
        "currency": currency,
        "account_id": inp.account_id,
        "notes": inp.notes or None,
        "fx_rate": cur["fx_rate"],
        "amount_inr": cur["amount_inr"],
        "fx_rate_date": cur["fx_rate_date"],
        "fx_source": cur["fx_source"],
    }
    if money_changed:
        keep_override = (
            cur["fx_source"] == "user_override"
            and currency == cur["currency"]
            and currency != "INR"
        )
        f = resolve_fx(fx, amount, currency, inp.date, cur["fx_rate"] if keep_override else None)
        params.update(
            fx_rate=f.fx_rate,
            amount_inr=f.amount_inr,
            fx_rate_date=f.fx_rate_date,
            fx_source=f.fx_source,
        )
    s.execute(
        text("""
        UPDATE transactions SET date = :date, raw_description = :description, merchant_norm = :merchant,
          amount = :amount, currency = :currency, account_id = :account_id, notes = :notes,
          fx_rate = :fx_rate, amount_inr = :amount_inr, fx_rate_date = :fx_rate_date, fx_source = :fx_source
        WHERE id = :id AND user_id = :u"""),
        params,
    )
    if "category_id" in changes:
        set_category(
            s, user_id, txn_id, changes["category_id"], changes.get("apply_to_merchant", False)
        )
    return get(s, user_id, txn_id)


def delete(s: Session, user_id: uuid.UUID, txn_id: uuid.UUID) -> None:
    _require_txn(s, user_id, txn_id)
    s.execute(
        text("DELETE FROM transactions WHERE id = :id AND user_id = :u"),
        {"id": txn_id, "u": user_id},
    )


def set_category(
    s: Session,
    user_id: uuid.UUID,
    txn_id: uuid.UUID,
    category_id: uuid.UUID | None,
    apply_to_merchant: bool = False,
) -> int:
    """User correction. With `apply_to_merchant`, remember it for the merchant (future rows) and
    re-label past rows of the same merchant that the user has not set manually. Returns the
    number of OTHER transactions changed."""
    cur = _require_txn(s, user_id, txn_id)
    if category_id is not None:
        try:
            categories.require_visible(s, user_id, category_id)
        except categories.CategoryError as e:
            raise TxnError(str(e), e.status) from e
    s.execute(
        text("""UPDATE transactions SET category_id = :c, cat_source = CASE WHEN :c IS NULL THEN NULL ELSE 'user' END,
                      cat_confidence = CASE WHEN :c IS NULL THEN NULL ELSE 1 END, cat_rationale = NULL,
                      needs_review = false WHERE id = :id AND user_id = :u"""),
        {"c": category_id, "id": txn_id, "u": user_id},
    )
    merchant = cur["merchant_norm"]
    if not (apply_to_merchant and merchant and category_id):
        return 0
    s.execute(
        text("""INSERT INTO merchant_memory (user_id, merchant_norm, category_id, source, hits)
                      VALUES (:u, :m, :c, 'user', 1)
                      ON CONFLICT (user_id, merchant_norm) DO UPDATE SET category_id = EXCLUDED.category_id,
                        source = 'user', hits = merchant_memory.hits + 1, updated_at = now()"""),
        {"u": user_id, "m": merchant, "c": category_id},
    )
    return rowcount(
        s.execute(
            text("""UPDATE transactions SET category_id = :c, cat_source = 'memory', cat_confidence = 0.95,
                      cat_rationale = 'You categorized this merchant', needs_review = false
                      WHERE user_id = :u AND merchant_norm = :m AND id <> :id
                        AND cat_source IS DISTINCT FROM 'user' AND category_id IS DISTINCT FROM :c"""),
            {"c": category_id, "u": user_id, "m": merchant, "id": txn_id},
        )
    )


def override_fx(
    s: Session, user_id: uuid.UUID, txn_id: uuid.UUID, rate: Decimal | None, fx: FXService | None
) -> dict[str, Any]:
    """Set an exact INR-per-unit rate for one transaction (or clear it with None → re-resolve)."""
    cur = _require_txn(s, user_id, txn_id)
    if cur["currency"] == get_currency_registry().base:
        raise TxnError("INR transactions need no exchange rate.", 422)
    f = resolve_fx(fx, cur["amount"], cur["currency"], cur["date"], rate)
    s.execute(
        text("""UPDATE transactions SET fx_rate = :r, amount_inr = :ai, fx_rate_date = :d, fx_source = :src
                      WHERE id = :id AND user_id = :u"""),
        {
            "r": f.fx_rate,
            "ai": f.amount_inr,
            "d": f.fx_rate_date,
            "src": f.fx_source,
            "id": txn_id,
            "u": user_id,
        },
    )
    return get(s, user_id, txn_id)


# ---------------------------------------------------------------------------- reads
_SELECT = """
SELECT v.id, v.date, v.raw_description AS description, v.merchant_norm, v.amount, v.currency, v.fx_rate,
  v.amount_inr, v.fx_rate_date, v.fx_source, v.amount_reporting, v.reporting_currency, v.category_id,
  c.name AS category_name, c.color AS category_color, c.icon AS category_icon, c.flow_type,
  v.cat_source, v.cat_confidence, v.cat_rationale, v.needs_review, v.notes, v.source, v.account_id,
  a.name AS account_name, v.recurring_rule_id, v.import_batch_id, v.created_at
FROM v_transactions_reporting v
LEFT JOIN categories c ON c.id = v.category_id
LEFT JOIN accounts a ON a.id = v.account_id
"""
SORTS = {
    "date_desc": "v.date DESC, v.created_at DESC",
    "date_asc": "v.date ASC, v.created_at ASC",
    "amount_desc": "abs(v.amount_reporting) DESC NULLS LAST",
    "amount_asc": "abs(v.amount_reporting) ASC",
}


def get(s: Session, user_id: uuid.UUID, txn_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text(_SELECT + " WHERE v.id = :id AND v.user_id = :u"), {"id": txn_id, "u": user_id}
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TxnError("Transaction not found.", 404)
    return dict(row)


# Quick period filters on the Transactions page ("" = any time, "custom" = the From/To dates).
PERIODS: dict[str, str] = {
    "": "Any time",
    "this_month": "This month",
    "last_month": "Last month",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "this_year": "This year",
    "last_year": "Last year",
    "custom": "Custom range",
}


def period_range(period: str, today: dt.date) -> tuple[dt.date, dt.date] | None:
    """Inclusive (from, to) for a preset period, in the user's local calendar; None otherwise.
    Calendar periods cover the whole month/year, so future-dated entries in it are included."""
    if period == "this_month":
        start = today.replace(day=1)
        return start, (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    if period == "last_month":
        end = today.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1), end
    if period in ("7d", "30d", "90d"):
        return today - dt.timedelta(days=int(period[:-1]) - 1), today
    if period == "this_year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    if period == "last_year":
        return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    return None


@dataclass
class TxnFilter:
    q: str | None = None
    category_id: uuid.UUID | None = None
    uncategorized: bool = False
    account_id: uuid.UUID | None = None
    kind: Kind | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    needs_review: bool | None = None
    source: str | None = None
    import_batch_id: uuid.UUID | None = None
    ids: list[uuid.UUID] | None = None  # evidence links from insights
    sort: str = "date_desc"
    limit: int = 50
    offset: int = 0


def _where(f: TxnFilter, user_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = ["v.user_id = :u"]
    p: dict[str, Any] = {"u": user_id}
    if f.q:
        clauses.append(
            "(v.raw_description ILIKE :q ESCAPE '\\' OR v.merchant_norm ILIKE :q ESCAPE '\\' "
            "OR v.notes ILIKE :q ESCAPE '\\')"
        )
        p["q"] = f"%{categories._like_escape(f.q.strip())}%"
    if f.uncategorized:
        clauses.append("v.category_id IS NULL")
    elif f.category_id:
        clauses.append("v.category_id = :cat")
        p["cat"] = f.category_id
    if f.account_id:
        clauses.append("v.account_id = :acct")
        p["acct"] = f.account_id
    if f.kind:
        clauses.append("v.amount < 0" if f.kind == "expense" else "v.amount > 0")
    if f.date_from:
        clauses.append("v.date >= :df")
        p["df"] = f.date_from
    if f.date_to:
        clauses.append("v.date <= :dt")
        p["dt"] = f.date_to
    if f.needs_review is not None:
        clauses.append("v.needs_review = :nr")
        p["nr"] = f.needs_review
    if f.source:
        clauses.append("v.source = :src")
        p["src"] = f.source
    if f.import_batch_id:
        clauses.append("v.import_batch_id = :ib")
        p["ib"] = f.import_batch_id
    if f.ids:
        clauses.append("v.id = ANY(:ids)")
        p["ids"] = list(f.ids)[:100]
    return " AND ".join(clauses), p


def list_(s: Session, user_id: uuid.UUID, f: TxnFilter) -> tuple[list[dict[str, Any]], int]:
    where, p = _where(f, user_id)
    order = SORTS.get(f.sort, SORTS["date_desc"])
    # `where` is built only from fixed clause strings (values are bound); `order` is allowlisted.
    count_sql = f"SELECT count(*) FROM v_transactions_reporting v WHERE {where}"  # noqa: S608
    page_sql = f"{_SELECT} WHERE {where} ORDER BY {order} LIMIT :lim OFFSET :off"
    total = s.execute(text(count_sql), p).scalar_one()
    rows = s.execute(
        text(page_sql), {**p, "lim": max(1, min(f.limit, 500)), "off": max(0, f.offset)}
    ).mappings()
    return [dict(r) for r in rows], int(total)


# ---------------------------------------------------------------------------- export
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> str:
    """CSV/Excel formula-injection guard for TEXT cells (never applied to numbers)."""
    v = "" if value is None else str(value)
    return "'" + v if v.startswith(_FORMULA) else v


def export_csv(s: Session, user_id: uuid.UUID, f: TxnFilter) -> str:
    f.limit, f.offset = 50_000, 0
    rows, _ = list_(s, user_id, f)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "date",
            "description",
            "amount",
            "type",
            "currency",
            "category",
            "account",
            "notes",
            "fx_rate",
            "amount_inr",
            "fx_rate_date",
            "fx_source",
            "amount_reporting",
            "reporting_currency",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["date"].isoformat(),
                safe_cell(r["description"]),
                str(abs(r["amount"])),
                "expense" if r["amount"] < 0 else "income",
                r["currency"],
                safe_cell(r["category_name"]),
                safe_cell(r["account_name"]),
                safe_cell(r["notes"]),
                str(r["fx_rate"]),
                str(r["amount_inr"]),
                r["fx_rate_date"].isoformat(),
                r["fx_source"],
                "" if r["amount_reporting"] is None else str(r["amount_reporting"]),
                r["reporting_currency"],
            ]
        )
    return buf.getvalue()
