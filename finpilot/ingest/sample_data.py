""" "Load sample data": one of the committed datasets in data/sample/ (INR only, USD only, or INR + USD),
imported as an undoable import batch (filename `sample-data`).

The files hold fixed dates (2026-04-01 → 2026-10-15). They are moved forward by whole MONTHS (not
days) so the last month lines up with the user's current month and salary stays on the 1st, rent
on the 3rd, etc. Rows that would land after today are dropped.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import json
import uuid
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.fx.service import FXService
from finpilot.recurring import service as rec
from finpilot.transactions import service as txns

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


class SampleDataError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@lru_cache
def manifest() -> dict[str, Any]:
    return dict(yaml.safe_load((SAMPLE_DIR / "datasets.yml").read_text(encoding="utf-8")))


def datasets() -> list[dict[str, Any]]:
    """[{key, label, description, currencies}] for the UI and GET /me/sample-data."""
    return [
        {"key": k, **{f: v[f] for f in ("label", "description", "currencies")}}
        for k, v in manifest()["datasets"].items()
    ]


def default_dataset(reporting_currency: str) -> str:
    return str(manifest()["default_for_currency"].get(reporting_currency, "inr"))


def read_rows(key: str) -> list[dict[str, str]]:
    ds = manifest()["datasets"][key]
    with (SAMPLE_DIR / ds["file"]).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _add_months(d: dt.date, k: int) -> dt.date:
    n = d.year * 12 + d.month - 1 + k
    y, m = divmod(n, 12)
    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def remove_rules(s: Session, user_id: uuid.UUID, batch: dict[str, Any]) -> None:
    """Undo of a sample-data batch also deletes the recurring rules it declared (and anything
    they have materialized since)."""
    for rid in (batch.get("row_counts_json") or {}).get("recurring_rule_ids", []):
        for sql in (
            "DELETE FROM transactions WHERE recurring_rule_id = :r AND user_id = :u",
            "DELETE FROM recurring_rules WHERE id = :r AND user_id = :u",
        ):
            s.execute(text(sql), {"r": rid, "u": user_id})


def loaded_dataset(s: Session, user_id: uuid.UUID) -> str | None:
    row = s.execute(
        text(
            "SELECT file_sha256 FROM import_batches WHERE user_id = :u AND filename = 'sample-data' "
            "AND status = 'imported' LIMIT 1"
        ),
        {"u": user_id},
    ).first()
    return str(row[0]).removeprefix("sample:") if row else None


def load_sample(
    s: Session, user_id: uuid.UUID, fx: FXService | None, dataset: str | None = None
) -> dict[str, Any]:
    if loaded_dataset(s, user_id) is not None:
        raise SampleDataError("Sample data is already loaded. Undo it from the Import page first.")
    cur = s.execute(
        text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    key = dataset or default_dataset(cur)
    if key not in manifest()["datasets"]:
        raise SampleDataError(f"Unknown sample dataset: {key}", 422)
    ds = manifest()["datasets"][key]
    raw = read_rows(key)
    today = rec.user_today(s, user_id)
    last = max(dt.date.fromisoformat(r["date"]) for r in raw)
    k = (today.year * 12 + today.month) - (last.year * 12 + last.month)
    rows = []
    for r in raw:
        d = _add_months(dt.date.fromisoformat(r["date"]), k)
        if d <= today:
            rows.append((d, r))

    batch_id = s.execute(
        text("""INSERT INTO import_batches (user_id, filename, file_kind, file_sha256, status,
                                    imported_at) VALUES (:u, 'sample-data', 'csv', :h, 'importing', now()) RETURNING id"""),
        {"u": user_id, "h": f"sample:{key}"},
    ).scalar_one()
    foreign: dict[str, tuple[dt.date, dt.date]] = {}
    for d, r in rows:
        if r["currency"] != "INR":
            lo, hi = foreign.get(r["currency"], (d, d))
            foreign[r["currency"]] = (min(lo, d), max(hi, d))
    if fx is not None:
        for c, (lo, hi) in foreign.items():
            fx.backfill(c, lo, hi)
    names = categories.name_index(s, user_id)
    built = []
    for i, (d, r) in enumerate(rows):
        inp = txns.TxnInput(
            kind="income" if r["type"] == "income" else "expense",
            amount=Decimal(r["amount"]),
            date=d,
            description=r["description"],
            currency=r["currency"],
            category_id=names.get(r["category"].strip().lower()) if r["category"] else None,
            notes=r["notes"] or None,
        )
        built.append(
            txns.build_row(
                s,
                user_id,
                inp,
                fx,
                source="import",
                cat_source="import",
                import_batch_id=batch_id,
                hash_suffix=f"sample:{key}:{i}",
            )
        )
    inserted = txns.insert_rows(s, built)

    # The fixed items become recurring rules from their next occurrence (history has the past).
    rule_ids = []
    for item in ds["recurring"]:
        day = int(item["day"])
        nxt = today.replace(day=min(day, 28))
        if nxt <= today:
            nxt = _add_months(today.replace(day=1), 1).replace(day=min(day, 28))
        rule = rec.create_rule(
            s,
            user_id,
            {
                "kind": item["kind"],
                "description": item["description"],
                "amount": Decimal(str(item["amount"])),
                "currency": item["currency"],
                "frequency": "monthly",
                "day_of_month": day,
                "start_date": nxt,
                "category_id": names.get(str(item["category"]).lower()),
            },
        )
        rule_ids.append(str(rule["id"]))
    counts = {
        "imported": inserted,
        "recurring_rules": len(rule_ids),
        "dataset": key,
        "months": 6,
    }
    s.execute(
        text("""UPDATE import_batches SET status = 'imported', row_counts_json = CAST(:c AS jsonb)
                      WHERE id = :b AND user_id = :u"""),
        {"c": json.dumps({**counts, "recurring_rule_ids": rule_ids}), "b": batch_id, "u": user_id},
    )
    return {**counts, "batch_id": batch_id}
