"""Loads a synthetic persona into Postgres as a flagged synthetic user (is_synthetic = true).

Synthetic users cannot log in (unusable password hash), are excluded from product features
(first-admin count, schedulers) and are deleted at the end of every eval run. Their FX rates
come from the seeded synthetic series stored with fx_rates.scope = 'synthetic'.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import create_engine, text

from eval.synth.generator import Dataset, SynthTxn
from finpilot.categories import service as cats
from finpilot.config import get_settings
from finpilot.db.session import user_session
from finpilot.ingest.normalizer import normalize_merchant

SYNTH_DOMAIN = "synthetic.finpilot.local"


@dataclass
class Loaded:
    user_id: uuid.UUID
    txn_ids: dict[str, uuid.UUID]  # synthetic id → transactions.id
    category_ids: dict[str, uuid.UUID]  # truth label (slug or custom name) → categories.id
    category_names: dict[uuid.UUID, str]  # categories.id → truth label


def _admin():
    return create_engine(get_settings().database_admin_url)


def store_fx(ds: Dataset) -> None:
    eng = _admin()
    try:
        with eng.begin() as c:
            c.execute(
                text("""INSERT INTO fx_rates (scope, rate_date, quote_currency, rate_inr_per_unit, source)
                              VALUES ('synthetic', :d, 'USD', :r, 'synthetic') ON CONFLICT DO NOTHING"""),
                [{"d": d, "r": r} for d, r in ds.fx.items()],
            )
    finally:
        eng.dispose()


def _rate(ds: Dataset, d: dt.date) -> tuple[Decimal, dt.date]:
    for back in range(0, 10):
        day = d - dt.timedelta(days=back)
        if day in ds.fx:
            return ds.fx[day], day
    raise KeyError(f"no synthetic rate near {d}")


def opening_balance(ds: Dataset, rows: list[SynthTxn]) -> Decimal:
    """Enough that the loaded history never goes below zero, plus one month of typical income
    (personas don't define balances; with zero they'd look permanently overdrawn). Reporting
    currency; foreign rows converted at the synthetic rate."""
    cur, run, low = ds.persona.reporting_currency, Decimal(0), Decimal(0)
    income: dict[tuple[int, int], Decimal] = {}
    for t in sorted(rows, key=lambda t: (t.date, t.id)):
        amt = t.amount
        if t.currency != cur:
            rate, _ = _rate(ds, t.date)
            amt = amt * rate if cur == "INR" else amt / rate
        run += amt
        low = min(low, run)
        if amt > 0:
            income[(t.date.year, t.date.month)] = (
                income.get((t.date.year, t.date.month), Decimal(0)) + amt
            )
    month = sorted(income.values())[len(income) // 2] if income else Decimal(0)
    return (-low + month).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def load(
    ds: Dataset,
    run_tag: str,
    txns: list[SynthTxn] | None = None,
    with_opening_balance: bool = False,
) -> Loaded:
    p = ds.persona
    store_fx(ds)
    uid = uuid.uuid4()
    rows = txns if txns is not None else ds.history()
    with user_session(uid) as s:
        s.execute(
            text("""INSERT INTO users (id, email, password_hash, name, reporting_currency, timezone, role,
                            is_synthetic, onboarded_at)
                          VALUES (:id, :e, '!synthetic-no-login', :n, :c, :tz, 'user', true, now())"""),
            {
                "id": uid,
                "e": f"{p.name}.{run_tag}@{SYNTH_DOMAIN}",
                "n": p.name,
                "c": p.reporting_currency,
                "tz": "Asia/Kolkata" if p.region == "IN" else "America/Los_Angeles",
            },
        )
        s.execute(text("INSERT INTO user_settings (user_id) VALUES (:u)"), {"u": uid})
        s.execute(
            text("""INSERT INTO accounts (user_id, name, type, currency, opening_balance)
                    VALUES (:u, 'Main', 'bank', :c, :ob)"""),
            {
                "u": uid,
                "c": p.reporting_currency,
                "ob": opening_balance(ds, rows) if with_opening_balance else Decimal(0),
            },
        )
        for c in p.custom_categories:
            cats.create_category(s, uid, dict(c))
        batch: list[dict[str, Any]] = []
        for t in rows:
            if t.currency == "INR":
                rate, rate_date, src = Decimal(1), t.date, "base"
            else:
                rate, rate_date = _rate(ds, t.date)
                src = "synthetic"
            inr = (t.amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            key = hashlib.sha256(
                f"{t.date}|{t.amount}|{t.currency}|{t.description}".encode()
            ).hexdigest()[:32]
            batch.append(
                {
                    "u": uid,
                    "d": t.date,
                    "desc": t.description[:500],
                    "m": normalize_merchant(t.description) or None,
                    "a": t.amount,
                    "c": t.currency,
                    "r": rate,
                    "ai": inr,
                    "rd": rate_date,
                    "src": src,
                    "h": f"synth:{t.id}",
                    "k": key,
                }
            )
        s.execute(
            text("""INSERT INTO transactions (user_id, date, raw_description, merchant_norm, amount, currency,
                            fx_rate, amount_inr, fx_rate_date, fx_source, source, hash, dedupe_key)
                          VALUES (:u, :d, :desc, :m, :a, :c, :r, :ai, :rd, :src, 'import', :h, :k)"""),
            batch,
        )
        ids = {
            r.hash.removeprefix("synth:"): r.id
            for r in s.execute(
                text("SELECT id, hash FROM transactions WHERE user_id = :u"), {"u": uid}
            )
        }
        index = cats.name_index(s, uid)
        slugs = {
            r.slug: r.id
            for r in s.execute(text("SELECT id, slug FROM categories WHERE user_id IS NULL"))
        }
    labels = {t.category for t in ds.txns}
    cat_ids = {lab: slugs.get(lab) or index[lab.lower()] for lab in labels}
    return Loaded(uid, ids, cat_ids, {v: k for k, v in cat_ids.items()})


def cleanup(run_tag: str | None = None) -> int:
    """Delete synthetic users (of one run, or all). Cascades to all their rows."""
    pattern = f"%.{run_tag}@{SYNTH_DOMAIN}" if run_tag else f"%@{SYNTH_DOMAIN}"
    eng = _admin()
    try:
        with eng.begin() as c:
            return int(
                c.execute(
                    text("DELETE FROM users WHERE is_synthetic AND email LIKE :p"), {"p": pattern}
                ).rowcount
            )
    finally:
        eng.dispose()


def apply_truth_categories(ds: Dataset, ld: Loaded) -> None:
    """Label every loaded transaction with its TRUE category (oracle), so suites that measure
    analysis (anomalies, recurring) are not confounded by categorizer errors."""
    rows = [
        {"c": ld.category_ids[t.category], "t": ld.txn_ids[t.id], "u": ld.user_id}
        for t in ds.txns
        if t.id in ld.txn_ids
    ]
    with user_session(ld.user_id) as s:
        s.execute(
            text("""UPDATE transactions SET category_id = :c, cat_source = 'import', cat_confidence = 1
                    WHERE id = :t AND user_id = :u"""),
            rows,
        )
