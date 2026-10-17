"""Import wizard backend: upload → (map columns) → preview → confirm (Celery) → undo.

The raw upload is stored in `import_files` until the batch is imported or undone, so the
user can re-map columns without re-uploading. FX rates for all foreign-currency rows are
backfilled with ONE range call per currency before preview/import.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.db.session import rowcount
from finpilot.fx.service import FXService
from finpilot.ingest import csv_parser as P
from finpilot.money import get_currency_registry, quantize
from finpilot.recurring.service import user_today
from finpilot.transactions import service as txns

log = logging.getLogger(__name__)
PREVIEW_SAMPLE = 20
PREVIEW_ERRORS = 100
INSERT_CHUNK = 1000


class ImportError_(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _batch(s: Session, user_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("SELECT * FROM import_batches WHERE id = :b AND user_id = :u"),
            {"b": batch_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ImportError_("Import not found.", 404)
    return dict(row)


def _table(s: Session, user_id: uuid.UUID, b: dict[str, Any]) -> P.Table:
    content = s.execute(
        text("SELECT content FROM import_files WHERE batch_id = :b AND user_id = :u"),
        {"b": b["id"], "u": user_id},
    ).scalar()
    if content is None:
        raise ImportError_("The uploaded file is no longer available.", 410)
    return P.read_table(bytes(content), b["filename"])


def _context(s: Session, user_id: uuid.UUID) -> P.ParseContext:
    reporting = s.execute(
        text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    accts = s.execute(
        text("SELECT id, name, currency FROM accounts WHERE user_id = :u AND NOT archived"),
        {"u": user_id},
    ).all()
    return P.ParseContext(
        category_index=categories.name_index(s, user_id),
        account_index={a.name.lower(): (a.id, a.currency) for a in accts},
        supported_currencies=set(get_currency_registry().codes),
        reporting_currency=reporting,
        today=user_today(s, user_id),
    )


# ---------------------------------------------------------------------------- upload
def create_batch(s: Session, user_id: uuid.UUID, filename: str, content: bytes) -> dict[str, Any]:
    filename = (filename or "upload.csv").replace("\\", "/").rsplit("/", 1)[-1][:200]
    try:
        table = P.read_table(content, filename)
    except P.ImportFileError as e:
        raise ImportError_(str(e), 422) from e
    mapping = P.guess_mapping(table.headers)
    date_col = mapping.get("date")
    formats = (
        P.detect_date_formats([r[date_col] for r in table.rows]) if date_col is not None else []
    )
    amt_cols = [c for k in ("amount", "debit", "credit") if (c := mapping.get(k)) is not None]
    amount_values = [r[c] for r in table.rows[:500] for c in amt_cols]
    options = {
        "date_format": None if P.is_ambiguous(formats) or not formats else formats[0],
        "date_format_candidates": formats,
        "decimal_comma": P.looks_decimal_comma(amount_values),
        "amount_sign": "negative_is_expense",
        "include_duplicates": False,
    }
    bid = s.execute(
        text("""
        INSERT INTO import_batches (user_id, filename, file_kind, file_sha256, mapping_json, options_json)
        VALUES (:u, :f, :k, :h, CAST(:m AS jsonb), CAST(:o AS jsonb)) RETURNING id"""),
        {
            "u": user_id,
            "f": filename,
            "k": table.kind,
            "h": hashlib.sha256(content).hexdigest(),
            "m": json.dumps(mapping),
            "o": json.dumps(options),
        },
    ).scalar_one()
    s.execute(
        text("INSERT INTO import_files (batch_id, user_id, content) VALUES (:b, :u, :c)"),
        {"b": bid, "u": user_id, "c": content},
    )
    return _batch(s, user_id, bid)


def update_mapping(
    s: Session,
    user_id: uuid.UUID,
    batch_id: uuid.UUID,
    mapping: dict[str, Any] | None,
    options: dict[str, Any] | None,
) -> dict[str, Any]:
    b = _batch(s, user_id, batch_id)
    if b["status"] != "uploaded":
        raise ImportError_("This import can no longer be changed.", 409)
    table = _table(s, user_id, b)
    new_map = b["mapping_json"]
    if mapping is not None:
        try:
            new_map = P.validate_mapping(mapping, len(table.headers))
        except P.ImportFileError as e:
            raise ImportError_(str(e), 422) from e
    opts = dict(b["options_json"])
    for k, v in (options or {}).items():
        if k == "date_format":
            if v not in (None, "", *P.DATE_FORMATS):
                raise ImportError_("Unknown date format.", 422)
            opts[k] = v or None
        elif k == "amount_sign":
            if v not in ("negative_is_expense", "positive_is_expense"):
                raise ImportError_("Unknown amount sign convention.", 422)
            opts[k] = v
        elif k in ("decimal_comma", "include_duplicates"):
            opts[k] = bool(v)
    if mapping is not None and new_map.get("date") is not None:
        opts["date_format_candidates"] = P.detect_date_formats(
            [r[new_map["date"]] for r in table.rows]
        )
    s.execute(
        text(
            "UPDATE import_batches SET mapping_json = CAST(:m AS jsonb), options_json = CAST(:o AS jsonb) "
            "WHERE id = :b AND user_id = :u"
        ),
        {"m": json.dumps(new_map), "o": json.dumps(opts), "b": batch_id, "u": user_id},
    )
    return _batch(s, user_id, batch_id)


# ---------------------------------------------------------------------------- analysis
def _analyze(
    s: Session, user_id: uuid.UUID, b: dict[str, Any], fx: FXService | None
) -> tuple[list[P.ParsedRow], dict[int, dict[str, Any]], dict[str, int]]:
    """Parse all rows, resolve FX, classify duplicates. Returns (rows, extras by row_no, counts)."""
    table = _table(s, user_id, b)
    opts = b["options_json"]
    if not opts.get("date_format") and P.is_ambiguous(opts.get("date_format_candidates") or []):
        raise ImportError_(
            "Dates are ambiguous (day/month vs month/day). Choose the date format first.", 409
        )
    rows = P.parse_rows(
        table, {k: v for k, v in b["mapping_json"].items()}, opts, _context(s, user_id)
    )
    valid = [r for r in rows if r.data]

    # FX: one range backfill per foreign currency, then per-(currency, date) resolution (cached).
    by_cur: dict[str, list[dt.date]] = defaultdict(list)
    for r in valid:
        assert r.data is not None
        if r.data["currency"] != "INR" and r.data["fx_rate"] is None:
            by_cur[r.data["currency"]].append(r.data["date"])
    rate_cache: dict[tuple[str, dt.date], txns.FxFields] = {}
    for cur, dates in by_cur.items():
        if fx is not None:
            try:
                fx.backfill(cur, min(dates), max(dates))
            except Exception:
                log.warning("FX backfill failed for %s", cur, exc_info=True)

    extras: dict[int, dict[str, Any]] = {}
    key_counts: Counter[str] = Counter()
    for r in valid:
        d = r.data
        assert d is not None
        signed = quantize(txns.signed(d["kind"], d["amount"]), d["currency"])
        ck = (d["currency"], d["date"])
        if d["currency"] == "INR" or d["fx_rate"] is not None:
            f = txns.resolve_fx(None, signed, d["currency"], d["date"], d["fx_rate"])
        else:
            if ck not in rate_cache:
                try:
                    rate_cache[ck] = txns.resolve_fx(fx, Decimal(1), d["currency"], d["date"])
                except Exception as e:
                    r.errors.append(f"No exchange rate available ({type(e).__name__}).")
                    r.data = None
                    continue
            base = rate_cache[ck]
            f = txns.FxFields(
                base.fx_rate,
                quantize(signed * base.fx_rate, "INR"),
                base.fx_rate_date,
                base.fx_source,
                base.stale,
                base.approximate,
            )
            if f.stale or f.approximate:
                r.warnings.append("Converted with stale/approximate rates.")
        key = txns.dedupe_key(d["date"], signed, d["currency"], d["description"], d["account_id"])
        extras[r.row_no] = {"signed": signed, "fx": f, "key": key, "occurrence": key_counts[key]}
        key_counts[key] += 1

    existing: dict[str, int] = {}
    if key_counts:
        for k, n in s.execute(
            text("""SELECT dedupe_key, count(*) FROM transactions WHERE user_id = :u
                                      AND dedupe_key = ANY(:keys) GROUP BY dedupe_key"""),
            {"u": user_id, "keys": list(key_counts)},
        ):
            existing[k] = int(n)
    counts = {"total": len(rows), "valid": 0, "new": 0, "duplicate": 0, "invalid": 0, "warnings": 0}
    for r in rows:
        if r.warnings:
            counts["warnings"] += 1
        if not r.data:
            counts["invalid"] += 1
            continue
        counts["valid"] += 1
        ex = extras[r.row_no]
        ex["duplicate"] = ex["occurrence"] < existing.get(ex["key"], 0)
        counts["duplicate" if ex["duplicate"] else "new"] += 1
    return rows, extras, counts


def preview(
    s: Session, user_id: uuid.UUID, batch_id: uuid.UUID, fx: FXService | None
) -> dict[str, Any]:
    b = _batch(s, user_id, batch_id)
    table = _table(s, user_id, b) if b["status"] == "uploaded" else None
    out: dict[str, Any] = {
        "batch": _public(b),
        "headers": table.headers if table else [],
        "mapping": b["mapping_json"],
        "options": b["options_json"],
    }
    if b["status"] != "uploaded":
        out.update(counts=b["row_counts_json"], rows=[], errors=b["errors_json"])
        return out
    opts = b["options_json"]
    if not opts.get("date_format") and P.is_ambiguous(opts.get("date_format_candidates") or []):
        out.update(
            needs_date_format=True,
            counts={},
            rows=[],
            errors=[],
            sample_raw=table.rows[:5] if table else [],
        )
        return out
    try:
        rows, extras, counts = _analyze(s, user_id, b, fx)
    except ImportError_ as err:
        out.update(counts={}, rows=[], errors=[{"row": 0, "errors": [str(err)]}])
        return out
    reporting = _context(s, user_id).reporting_currency
    sample: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for r in rows:
        if r.data and len(sample) < PREVIEW_SAMPLE:
            ex = extras[r.row_no]
            f: txns.FxFields = ex["fx"]
            sample.append(
                {
                    "row": r.row_no,
                    "date": r.data["date"],
                    "description": r.data["description"],
                    "kind": r.data["kind"],
                    "amount": ex["signed"],
                    "currency": r.data["currency"],
                    "amount_inr": f.amount_inr,
                    "fx_rate": f.fx_rate,
                    "fx_rate_date": f.fx_rate_date,
                    "fx_source": f.fx_source,
                    "category_id": r.data["category_id"],
                    "duplicate": ex["duplicate"],
                    "warnings": r.warnings,
                    "reporting_currency": reporting,
                }
            )
        if (r.errors or r.warnings) and len(errors) < PREVIEW_ERRORS:
            errors.append(
                {"row": r.row_no, "errors": r.errors, "warnings": r.warnings, "raw": r.raw[:8]}
            )
    dates = [r.data["date"] for r in rows if r.data]
    out.update(
        counts=counts,
        rows=sample,
        errors=errors,
        needs_date_format=False,
        date_range=[min(dates), max(dates)] if dates else None,
    )
    return out


def _public(b: dict[str, Any]) -> dict[str, Any]:
    return {
        k: b[k]
        for k in (
            "id",
            "filename",
            "file_kind",
            "status",
            "row_counts_json",
            "created_at",
            "imported_at",
            "undone_at",
        )
    }


# ---------------------------------------------------------------------------- confirm / run / undo
def mark_importing(s: Session, user_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    b = _batch(s, user_id, batch_id)
    if b["status"] != "uploaded":
        raise ImportError_(f"Import is already {b['status']}.", 409)
    s.execute(
        text("UPDATE import_batches SET status = 'importing' WHERE id = :b AND user_id = :u"),
        {"b": batch_id, "u": user_id},
    )
    return _batch(s, user_id, batch_id)


def run_import(
    s: Session, user_id: uuid.UUID, batch_id: uuid.UUID, fx: FXService | None
) -> dict[str, int]:
    """Executed by the Celery task inside a tenant session. All-or-nothing."""
    b = _batch(s, user_id, batch_id)
    if b["status"] != "importing":
        raise ImportError_(f"Import is {b['status']}, expected importing.", 409)
    b = {**b, "status": "uploaded"}  # analysis uses the same path as preview
    rows, extras, counts = _analyze(s, user_id, b, fx)
    include_dupes = bool(b["options_json"].get("include_duplicates"))
    resolver = categories.CategoryResolver(s, user_id)
    to_insert: list[dict[str, Any]] = []
    for r in rows:
        if not r.data:
            continue
        e = extras[r.row_no]
        if e["duplicate"] and not include_dupes:
            continue
        d = r.data
        inp = txns.TxnInput(
            kind=d["kind"],
            amount=d["amount"],
            date=d["date"],
            description=d["description"],
            currency=d["currency"],
            category_id=d["category_id"],
            account_id=d["account_id"],
            notes=d["notes"],
            fx_rate=d["fx_rate"],
        )
        row = txns.build_row(
            s,
            user_id,
            inp,
            None,
            source="import",
            cat_source="import",
            import_batch_id=batch_id,
            fx_fields=e["fx"],
            hash_suffix=f"i:{e['occurrence']}"
            if not e["duplicate"]
            else f"i:{batch_id}:{r.row_no}",
            resolver=resolver,
        )
        to_insert.append(row)
    inserted = 0
    for i in range(0, len(to_insert), INSERT_CHUNK):
        inserted += txns.insert_rows(s, to_insert[i : i + INSERT_CHUNK])
    counts = {
        **counts,
        "imported": inserted,
        "skipped_duplicates": 0 if include_dupes else counts["duplicate"],
    }
    errors = [{"row": r.row_no, "errors": r.errors} for r in rows if r.errors][:PREVIEW_ERRORS]
    s.execute(
        text("""UPDATE import_batches SET status = 'imported', imported_at = now(),
                      row_counts_json = CAST(:c AS jsonb), errors_json = CAST(:e AS jsonb)
                      WHERE id = :b AND user_id = :u"""),
        {
            "c": json.dumps(counts),
            "e": json.dumps(errors, default=str),
            "b": batch_id,
            "u": user_id,
        },
    )
    s.execute(
        text("DELETE FROM import_files WHERE batch_id = :b AND user_id = :u"),
        {"b": batch_id, "u": user_id},
    )
    return counts


def mark_failed(s: Session, user_id: uuid.UUID, batch_id: uuid.UUID, error: str) -> None:
    s.execute(
        text("""UPDATE import_batches SET status = 'failed', errors_json = CAST(:e AS jsonb)
                      WHERE id = :b AND user_id = :u"""),
        {"e": json.dumps([{"row": 0, "errors": [error[:300]]}]), "b": batch_id, "u": user_id},
    )


def undo(s: Session, user_id: uuid.UUID, batch_id: uuid.UUID) -> int:
    b = _batch(s, user_id, batch_id)
    if b["status"] != "imported":
        raise ImportError_("Only completed imports can be undone.", 409)
    if b["filename"] == "sample-data":
        from finpilot.ingest.sample_data import remove_rules

        remove_rules(s, user_id, b)
    removed = s.execute(
        text("DELETE FROM transactions WHERE user_id = :u AND import_batch_id = :b"),
        {"u": user_id, "b": batch_id},
    )
    s.execute(
        text(
            "UPDATE import_batches SET status = 'undone', undone_at = now() WHERE id = :b AND user_id = :u"
        ),
        {"b": batch_id, "u": user_id},
    )
    return rowcount(removed)


def list_batches(s: Session, user_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
    rows = s.execute(
        text("SELECT * FROM import_batches WHERE user_id = :u ORDER BY created_at DESC LIMIT :l"),
        {"u": user_id, "l": limit},
    ).mappings()
    return [_public(dict(r)) for r in rows]
