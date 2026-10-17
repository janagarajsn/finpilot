"""`fx_exposure` (Amendment 1): share of spend in currencies other than the reporting currency, and
the reporting-currency effect of rate moves on foreign recurring items since their first charge.

Decomposition per series (exact by construction, amounts in the reporting currency):
    cost_last − cost_first = price_effect + fx_effect
    price_effect = (orig_last − orig_first) × rate_first
    fx_effect    = orig_last × (rate_last − rate_first)
where rate = reporting units per original unit at each charge's own rate date (stored on the
transaction / reporting view). No rate is invented: only rates already stored are used.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pandas as pd

from finpilot.insights.frame import Frame
from finpilot.insights.models import Finding

WINDOW_DAYS = 90
MIN_EFFECT_SHARE = 0.01  # series effect below 1% of its cost is noise
PERIOD_NOUN = {
    "weekly": "week",
    "biweekly": "two weeks",
    "monthly": "month",
    "quarterly": "quarter",
    "yearly": "year",
}
PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}


def _rate_label(rate: Decimal, rep: str, orig: str) -> str:
    if rate >= 1:
        return f"{rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)} {rep} per {orig}"
    inv = (Decimal(1) / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{inv} {orig} per {rep}"


def series_effect(fr: Frame, d: dict[str, Any]) -> dict[str, Any] | None:
    rows = fr.df[fr.df["id"].isin([str(i) for i in d["txn_ids_json"]])].sort_values(["date", "id"])
    if len(rows) < 2:
        return None
    first, last = rows.iloc[0], rows.iloc[-1]
    of, ol = Decimal(str(abs(first["amount"]))), Decimal(str(abs(last["amount"])))
    cf, cl = (
        Decimal(str(abs(first["amount_reporting"]))),
        Decimal(str(abs(last["amount_reporting"]))),
    )
    rate_f, rate_l = cf / of, cl / ol
    total = cl - cf
    price = fr.money((ol - of) * rate_f)
    fx_eff = fr.money(total) - price  # exact: price + fx == total
    return {
        "detected_id": str(d["id"]),
        "merchant": d["merchant_norm"],
        "orig_currency": d["currency"],
        "period": d["period"],
        "orig_amount": fr.money(ol, d["currency"]),
        "first_date": first["date"].date().isoformat(),
        "last_date": last["date"].date().isoformat(),
        "first_cost": fr.money(cf),
        "last_cost": fr.money(cl),
        "price_effect": price,
        "fx_effect": fx_eff,
        "fx_effect_monthly": fr.money(fx_eff * PER_YEAR[d["period"]] / 12),
        "rate_first": _rate_label(rate_f, fr.currency, d["currency"]),
        "rate_last": _rate_label(rate_l, fr.currency, d["currency"]),
        "first_txn_id": str(first["id"]),
        "last_txn_id": str(last["id"]),
    }


def run(fr: Frame) -> tuple[dict[str, Any] | None, list[Finding]]:
    start = fr.as_of - dt.timedelta(days=WINDOW_DAYS - 1)
    sp = fr.spending()
    sp = sp[sp["date"] >= pd.Timestamp(start)]
    total = float(-sp["amt"].sum())
    foreign = sp[sp["currency"] != fr.currency]
    if total <= 0 or foreign.empty:
        return None, []
    f_total = float(-foreign["amt"].sum())
    by_cur = {c: fr.money(-g["amt"].sum()) for c, g in foreign.groupby("currency")}
    series = [
        e
        for d in fr.detected
        if d["kind"] == "expense" and d["status"] == "active" and d["currency"] != fr.currency
        for e in [series_effect(fr, d)]
        if e is not None
    ]
    monthly_fx = sum((s["fx_effect_monthly"] for s in series), Decimal(0)) if series else None
    share = round(f_total / total * 100, 1)
    report = {
        "window_days": WINDOW_DAYS,
        "total_spend": fr.money(total),
        "foreign_spend": fr.money(f_total),
        "share_pct": share,
        "by_currency": by_cur,
        "series": series,
        "fx_effect_monthly": monthly_fx,
        "currency": fr.currency,
    }
    findings = [
        Finding(
            fingerprint=f"fx_exposure:{','.join(sorted(by_cur))}",
            kind="fx_exposure",
            type="fx_exposure",
            severity="low" if share >= 10 else "info",
            metric="foreign_currency_share_pct",
            baseline=None,
            observed=Decimal(str(share)),
            threshold=None,
            unit="pct",
            currency=fr.currency,
            evidence_txn_ids=[str(i) for i in foreign.sort_values("amt")["id"].head(10)],
            data={
                "share": share,
                "foreign_spend": fr.money(f_total),
                "total_spend": fr.money(total),
                "currency": fr.currency,
                "currencies": ", ".join(sorted(by_cur)),
                "days": WINDOW_DAYS,
                "fx_effect_month": monthly_fx,
            },
            period_start=start,
            period_end=fr.as_of,
        )
    ]
    for s in series:
        if s["first_cost"] == 0 or abs(s["fx_effect"]) / s["first_cost"] < Decimal(
            str(MIN_EFFECT_SHARE)
        ):
            continue
        findings.append(
            Finding(
                fingerprint=f"fx_series_effect:{s['detected_id']}",
                kind="fx_exposure",
                type="fx_series_effect",
                severity="info" if s["fx_effect"] < 0 else "low",
                metric="fx_effect_per_charge",
                baseline=s["first_cost"],
                observed=s["last_cost"],
                threshold=None,
                unit="currency",
                currency=fr.currency,
                evidence_txn_ids=[s["first_txn_id"], s["last_txn_id"]],
                data={
                    **s,
                    "baseline": s["first_cost"],
                    "observed": s["last_cost"],
                    "currency": fr.currency,
                    "period_noun": PERIOD_NOUN[s["period"]],
                    "first_date": dt.date.fromisoformat(s["first_date"]).strftime("%d %b %Y"),
                },
            )
        )
    return report, findings
