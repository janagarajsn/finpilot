"""Eval-only: a 6-month history from the seeded persona generator, imported as an undoable batch
(filename `sample-data`), used by the cache and memory suites. (The product's "Load sample data"
uses the committed files in data/sample/ instead: finpilot/ingest/sample_data.py.)"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories import service as categories
from finpilot.fx.service import FXService
from finpilot.recurring import service as rec
from finpilot.transactions import service as txns

PERSONA_FOR = {"INR": "inr_salaried_emi_sip", "USD": "usd_salaried_stable"}
MONTHS = 6
RECURRING_NAMES = {
    "salary": "Salary",
    "payroll": "Payroll",
    "rent": "Rent",
    "netflix": "Netflix",
    "sip": "SIP investment",
    "emi": "Loan EMI",
    "car_loan": "Car loan",
}


class SampleDataError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def load_sample(s: Session, user_id: uuid.UUID, fx: FXService | None) -> dict[str, Any]:
    from eval.synth.generator import _add_months, generate
    from eval.synth.personas import get_persona

    if s.execute(
        text(
            "SELECT 1 FROM import_batches WHERE user_id = :u AND filename = 'sample-data' "
            "AND status = 'imported'"
        ),
        {"u": user_id},
    ).first():
        raise SampleDataError("Sample data is already loaded. Undo it from the Import page first.")
    cur = s.execute(
        text("SELECT reporting_currency FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    p = get_persona(PERSONA_FOR.get(cur, "inr_salaried_emi_sip"))
    ds = generate(p)
    hist = ds.history()
    last = max(t.date for t in hist)
    first_kept = _add_months(last.replace(day=1), -(MONTHS - 1))
    today = rec.user_today(s, user_id)
    # Shift by whole MONTHS (not days) so salary stays on the 1st, rent on the 3rd, etc. and matches
    # the recurring rules declared below; the current month is month-to-date (later rows dropped).
    k = (today.year * 12 + today.month) - (last.year * 12 + last.month)

    def moved(d: dt.date) -> dt.date:
        first = _add_months(d.replace(day=1), k)
        return first.replace(day=min(d.day, calendar.monthrange(first.year, first.month)[1]))

    # Credit-card bill payments duplicate the card purchases that are already listed one by one (a
    # single-account demo would count that spending twice), so the sample leaves them out.
    card_bills = {f"{p.name}:{x.key}" for x in p.series if x.category == "transfers"}
    rows = [
        t
        for t in hist
        if t.date >= first_kept
        and not t.injected  # prompt-injection probes are eval-only; planted duplicates/spikes stay (Insights demo)
        and t.series_id not in card_bills
        and moved(t.date) <= today
    ]

    batch_id = s.execute(
        text("""INSERT INTO import_batches (user_id, filename, file_kind, file_sha256, status,
                                    imported_at) VALUES (:u, 'sample-data', 'csv', :h, 'importing', now()) RETURNING id"""),
        {"u": user_id, "h": f"sample:{p.name}"},
    ).scalar_one()
    foreign: dict[str, tuple[dt.date, dt.date]] = {}
    for t in rows:
        if t.currency != "INR":
            d = moved(t.date)
            lo, hi = foreign.get(t.currency, (d, d))
            foreign[t.currency] = (min(lo, d), max(hi, d))
    if fx is not None:
        for c, (lo, hi) in foreign.items():
            fx.backfill(c, lo, hi)
    resolver = categories.CategoryResolver(s, user_id)
    built = []
    for i, t in enumerate(rows):
        inp = txns.TxnInput(
            kind="income" if t.amount > 0 else "expense",
            amount=abs(t.amount),
            date=moved(t.date),
            description=t.description,
            currency=t.currency,
        )
        built.append(
            txns.build_row(
                s,
                user_id,
                inp,
                fx,
                source="import",
                import_batch_id=batch_id,
                hash_suffix=f"sample:{i}",
                resolver=resolver,
            )
        )
    inserted = txns.insert_rows(s, built)

    # Declare the main fixed items as recurring rules starting from their next occurrence.
    rules = 0
    names = categories.name_index(s, user_id)
    slugs = {
        r.slug: r.id
        for r in s.execute(text("SELECT id, slug FROM categories WHERE user_id IS NULL"))
    }
    for series in p.series:
        if (
            series.key not in RECURRING_NAMES
            or series.period != "monthly"
            or series.currency != cur
        ):
            continue
        day = min(series.day, 28)
        nxt = today.replace(
            day=day
        )  # next occurrence strictly after today (history already has the past)
        if nxt <= today:
            nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=day)
        rec.create_rule(
            s,
            user_id,
            {
                "kind": series.kind,
                "description": RECURRING_NAMES[series.key],
                "amount": series.amount,
                "currency": series.currency,
                "frequency": "monthly",
                "day_of_month": series.day,
                "start_date": nxt,
                "category_id": slugs.get(series.category) or names.get(series.category.lower()),
            },
        )
        rules += 1
    counts = {"imported": inserted, "recurring_rules": rules, "persona": p.name, "months": MONTHS}
    s.execute(
        text("""UPDATE import_batches SET status = 'imported', row_counts_json = CAST(:c AS jsonb)
                      WHERE id = :b AND user_id = :u"""),
        {"c": json.dumps(counts), "b": batch_id, "u": user_id},
    )
    return {**counts, "batch_id": batch_id}
