"""Behavioral patterns over the recent window (deterministic). Every metric is reported in
`AnalysisReport.patterns` even when it does not cross its threshold; only crossings become findings.

There are no times of day in bank data, so "late-night" spending cannot be measured: the impulse
signal is a documented proxy (frequency of purchases in impulse-prone categories).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from finpilot.insights.frame import Frame
from finpilot.insights.models import Finding

WINDOW_DAYS = 90
MIN_DAYS = 28  # patterns need about a month of data


def _days_covered(fr: Frame, days: int) -> int:
    start = max(fr.as_of - dt.timedelta(days=days - 1), fr.history_start or fr.as_of)
    return (fr.as_of - start).days + 1


def _discretionary(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["amt"] < 0) & (df["nature"] == "discretionary")]


def weekend(fr: Frame, days: int) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    start = fr.as_of - dt.timedelta(days=days - 1)
    cal = pd.date_range(start, fr.as_of, freq="D")
    wknd_days = int((cal.dayofweek >= 5).sum())
    wk_days = len(cal) - wknd_days
    d = _discretionary(fr.window(days))
    is_wknd = d["date"].dt.dayofweek >= 5
    w_total, k_total = float(-d[is_wknd]["amt"].sum()), float(-d[~is_wknd]["amt"].sum())
    w_daily = w_total / max(wknd_days, 1)
    k_daily = k_total / max(wk_days, 1)
    ratio = w_daily / k_daily if k_daily > 0 else None
    m = {
        "weekend_daily": fr.money(w_daily),
        "weekday_daily": fr.money(k_daily),
        "ratio": round(ratio, 2) if ratio else None,
    }
    thr = float(cfg["weekend_ratio"])
    if ratio is None or ratio < thr or w_total < float(fr.thr.min_anomaly_amount):
        return m, None
    return m, Finding(
        fingerprint=f"weekend_spending:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="weekend_spending",
        severity="low",
        metric="weekend_to_weekday_daily_ratio",
        baseline=fr.money(k_daily),
        observed=fr.money(w_daily),
        threshold=Decimal(str(thr)),
        unit="currency",
        currency=fr.currency,
        evidence_txn_ids=[str(i) for i in d[is_wknd].sort_values("amt")["id"].head(10)],
        data={**m, "currency": fr.currency, "threshold": thr, "days": days},
        period_start=start,
        period_end=fr.as_of,
    )


def payday(fr: Frame, days: int) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    w = int(cfg["payday_window_days"])
    start = fr.as_of - dt.timedelta(days=days - 1)
    df = fr.window(days)
    inc = df[(df["amt"] > 0) & (df["flow_type"] == "income")]
    if inc.empty:
        return {"paydays": 0}, None
    big = inc[inc["amt"] >= 0.25 * inc.groupby(inc["date"].dt.to_period("M"))["amt"].sum().median()]
    pays = sorted({d for d in big["date"] if (fr.as_of - d.date()).days >= w - 1})
    var = df[
        (df["amt"] < 0)
        & (~df["flow_type"].isin(("transfer", "savings")))
        & (df["nature"] != "fixed")
    ]
    if len(pays) < 2 or var.empty:
        return {"paydays": len(pays)}, None
    after = [
        float(-var[(var["date"] >= p) & (var["date"] < p + pd.Timedelta(days=w))]["amt"].sum())
        for p in pays
    ]
    typical = float(-var["amt"].sum()) / ((fr.as_of - start).days + 1) * w
    ratio = float(np.mean(after)) / typical if typical > 0 else None
    m = {
        "paydays": len(pays),
        "after_payday": fr.money(np.mean(after)),
        "typical_window": fr.money(typical),
        "ratio": round(ratio, 2) if ratio else None,
        "window": w,
    }
    thr = float(cfg["payday_ratio"])
    if ratio is None or ratio < thr:
        return m, None
    ev = var[var["date"].isin([p + pd.Timedelta(days=i) for p in pays for i in range(w)])]
    return m, Finding(
        fingerprint=f"payday_spike:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="payday_spike",
        severity="low",
        metric="post_payday_window_spend_ratio",
        baseline=fr.money(typical),
        observed=fr.money(np.mean(after)),
        threshold=Decimal(str(thr)),
        unit="currency",
        currency=fr.currency,
        evidence_txn_ids=[str(i) for i in ev.sort_values("amt")["id"].head(10)],
        data={**m, "currency": fr.currency, "threshold": thr},
        period_start=start,
        period_end=fr.as_of,
    )


def impulse(fr: Frame, days: int) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    cats = set(cfg["impulse_categories"])
    df = fr.window(days)
    disc_total = float(-_discretionary(df)["amt"].sum())
    imp = df[(df["amt"] < 0) & (df["category_slug"].isin(cats))]
    per_week = len(imp) / (days / 7)
    share = float(-imp["amt"].sum()) / disc_total * 100 if disc_total > 0 else 0.0
    names = sorted(set(imp["category_name"]))
    m = {
        "count": len(imp),
        "per_week": round(per_week, 1),
        "total": fr.money(-imp["amt"].sum()),
        "share": round(share, 1),
    }
    thr = float(cfg.get("impulse_per_week", 5))
    if per_week < thr or share < 20:
        return m, None
    return m, Finding(
        fingerprint=f"impulse_proxy:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="impulse_proxy",
        severity="low",
        metric="impulse_purchases_per_week",
        baseline=None,
        observed=Decimal(str(round(per_week, 1))),
        threshold=Decimal(str(thr)),
        unit="count",
        currency=fr.currency,
        evidence_txn_ids=[str(i) for i in imp.sort_values("amt")["id"].head(10)],
        data={**m, "currency": fr.currency, "days": days, "categories": ", ".join(names)},
        period_start=fr.as_of - dt.timedelta(days=days - 1),
        period_end=fr.as_of,
    )


def concentration(fr: Frame, days: int) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    d = _discretionary(fr.window(days))
    total = float(-d["amt"].sum())
    if total <= 0:
        return {}, None
    by = (-d.groupby("category_name")["amt"].sum()).sort_values(ascending=False)
    top, amount = str(by.index[0]), float(by.iloc[0])
    share = amount / total * 100
    m = {
        "top_category": top,
        "share": round(share, 1),
        "amount": fr.money(amount),
        "total": fr.money(total),
    }
    thr = float(cfg["concentration_share"]) * 100
    if share < thr or len(by) < 2 or total < 3 * float(fr.thr.min_anomaly_amount):
        return m, None
    ev = d[d["category_name"] == top].sort_values("amt")["id"].head(10)
    return m, Finding(
        fingerprint=f"concentration:{top}:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="concentration",
        severity="low",
        metric="top_category_share_pct",
        baseline=None,
        observed=Decimal(str(round(share, 1))),
        threshold=Decimal(str(thr)),
        unit="pct",
        currency=fr.currency,
        evidence_txn_ids=[str(i) for i in ev],
        data={**m, "category": top, "currency": fr.currency, "threshold": round(thr), "days": days},
        period_start=fr.as_of - dt.timedelta(days=days - 1),
        period_end=fr.as_of,
    )


def _month_span(a: Any, b: Any) -> str:
    """'2026-04', '2026-06' → 'Apr–Jun 2026' (the year on both ends when it differs)."""
    da, db = (dt.date.fromisoformat(str(x)[:7] + "-01") for x in (a, b))
    if da.year == db.year:
        return f"{da:%b}–{db:%b %Y}"
    return f"{da:%b %Y}–{db:%b %Y}"


def savings_trend(fr: Frame) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    n = int(cfg["savings_trend_months"])
    df = fr.df[fr.df["date"] < pd.Timestamp(fr.as_of.replace(day=1))]  # full months only
    if df.empty:
        return {"months": []}, None
    mo = df["date"].dt.to_period("M")
    income = df[(df["amt"] > 0) & (df["flow_type"] == "income")].groupby(mo)["amt"].sum()
    spend = (
        -fr.spending()[fr.spending()["date"] < pd.Timestamp(fr.as_of.replace(day=1))]
        .groupby(mo)["amt"]
        .sum()
    )
    months = sorted(set(income.index) | set(spend.index))[-n:]
    rates = []
    for p in months:
        inc = float(income.get(p, 0.0))
        rates.append(
            (str(p), round((inc - float(spend.get(p, 0.0))) / inc * 100, 1) if inc > 0 else None)
        )
    m: dict[str, Any] = {"months": [{"month": p, "savings_rate": r} for p, r in rates]}
    valid = [r for _, r in rates if r is not None]
    if len(rates) < n or len(valid) < n:
        return m, None
    early, late = float(np.mean(valid[:3])), float(np.mean(valid[-3:]))
    drop = early - late
    m.update({"early": round(early, 1), "late": round(late, 1)})
    thr = float(cfg["savings_trend_drop_pts"])
    if drop < thr:
        return m, None
    return m, Finding(
        fingerprint=f"savings_rate_trend:{rates[-1][0]}",
        kind="trend",
        type="savings_rate_trend",
        severity="medium" if drop >= 2 * thr else "low",
        metric="savings_rate_drop_points",
        baseline=Decimal(str(round(early, 1))),
        observed=Decimal(str(round(late, 1))),
        threshold=Decimal(str(thr)),
        unit="pct",
        currency=fr.currency,
        data={
            **m,
            "drop": round(drop, 1),
            "threshold": thr,
            "early_label": _month_span(rates[0][0], rates[2][0]),
            "late_label": _month_span(rates[-3][0], rates[-1][0]),
        },
    )


def latte(fr: Frame, days: int) -> tuple[dict[str, Any], Finding | None]:
    cfg = fr.cfg["patterns"]
    ceiling = float(fr.thr.small_purchase_ceiling)
    sp = fr.spending()
    sp = sp[sp["date"] >= pd.Timestamp(fr.as_of - dt.timedelta(days=days - 1))]
    total = float(-sp["amt"].sum())
    small = sp[-sp["amt"] <= ceiling]
    s_total = float(-small["amt"].sum())
    share = s_total / total * 100 if total > 0 else 0.0
    m = {
        "count": len(small),
        "total": fr.money(s_total),
        "share": round(share, 1),
        "annualized": fr.money(s_total * 365 / days),
        "ceiling": fr.money(ceiling),
    }
    thr = float(cfg["latte_min_share"]) * 100
    if share < thr or s_total < float(fr.thr.min_anomaly_amount):
        return m, None
    return m, Finding(
        fingerprint=f"latte_factor:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="latte_factor",
        severity="low",
        metric="small_purchase_share_pct",
        baseline=None,
        observed=Decimal(str(round(share, 1))),
        threshold=Decimal(str(round(thr, 1))),
        unit="pct",
        currency=fr.currency,
        evidence_txn_ids=[
            str(i) for i in small.sort_values("date", ascending=False)["id"].head(10)
        ],
        data={**m, "currency": fr.currency, "threshold": round(thr, 1), "days": days},
        period_start=fr.as_of - dt.timedelta(days=days - 1),
        period_end=fr.as_of,
    )


def run(fr: Frame) -> tuple[dict[str, Any], list[Finding]]:
    days = min(WINDOW_DAYS, _days_covered(fr, WINDOW_DAYS))
    if days < MIN_DAYS:
        return {"window_days": days, "insufficient_data": True}, []
    metrics: dict[str, Any] = {"window_days": days}
    found: list[Finding] = []
    for name, fn in (
        ("weekend", lambda: weekend(fr, days)),
        ("payday", lambda: payday(fr, days)),
        ("impulse", lambda: impulse(fr, days)),
        ("concentration", lambda: concentration(fr, days)),
        ("savings_rate", lambda: savings_trend(fr)),
        ("latte", lambda: latte(fr, days)),
    ):
        m, f = fn()
        metrics[name] = m
        if f is not None:
            found.append(f)
    return metrics, found


def fees_paid(fr: Frame, days: int = 90) -> Finding | None:
    """Bank fees / card interest in the last `days` (Fees/Interest category): avoidable money."""
    sp = fr.spending()
    sel = sp[
        (sp["category_slug"] == "fees_interest")
        & (sp["date"] >= pd.Timestamp(fr.as_of - dt.timedelta(days=days - 1)))
    ]
    total = float(-sel["amt"].sum())
    if sel.empty or total < float(fr.thr.small_purchase_ceiling):
        return None
    monthly = total / (days / 30.44)
    top = sel.groupby("merchant_norm")["amt"].sum().sort_values().index[0] or "your bank"
    return Finding(
        fingerprint=f"fees_paid:{fr.as_of:%Y-%m}",
        kind="pattern",
        type="fees_paid",
        severity="low",
        metric="fees_and_interest_last_90d",
        baseline=None,
        observed=fr.money(total),
        threshold=fr.money(float(fr.thr.small_purchase_ceiling)),
        unit="currency",
        currency=fr.currency,
        evidence_txn_ids=[str(i) for i in sel.sort_values("amt")["id"].head(10)],
        data={
            "total": fr.money(total),
            "monthly": fr.money(monthly),
            "count": len(sel),
            "days": days,
            "merchant": top,
            "currency": fr.currency,
        },
        period_start=fr.as_of - dt.timedelta(days=days - 1),
        period_end=fr.as_of,
    )


def recurring_not_declared(fr: Frame) -> list[Finding]:
    """Confident detected repeats the user has not declared (feed "make it a recurring item")."""
    per = {
        "weekly": "week",
        "biweekly": "two weeks",
        "monthly": "month",
        "quarterly": "quarter",
        "yearly": "year",
    }
    out = []
    for d in fr.detected:
        if (
            d["status"] != "active"
            or d["dismissed"]
            or d["promoted_to_rule_id"] is not None
            or float(d["confidence"]) < 0.8
        ):
            continue
        out.append(
            Finding(
                fingerprint=f"recurring_not_declared:{d['series_key']}",
                kind="pattern",
                type="recurring_not_declared",
                severity="info",
                metric="charges_detected",
                observed=Decimal(d["n_occurrences"]),
                unit="count",
                currency=d["currency"],
                evidence_txn_ids=[str(i) for i in d["txn_ids_json"][-3:]],
                data={
                    "merchant": d["merchant_norm"],
                    "amount": d["amount_typical"],
                    "currency": d["currency"],
                    "period_noun": per[d["period"]],
                    "count": d["n_occurrences"],
                    "next_expected": d["next_expected"].isoformat(),
                    "detected_id": str(d["id"]),
                    "kind": d["kind"],
                },
            )
        )
    return out
