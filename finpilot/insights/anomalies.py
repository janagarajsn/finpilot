"""Anomaly detectors (deterministic). Each finding states metric, baseline, observed, threshold.

Transaction level (one finding per transaction; priority first_time_large > merchant_outlier >
category_outlier): duplicate charges, merchant/category outliers (robust z on log amounts,
leave-one-out baseline), first-time large merchants, price hikes on detected recurring series.
Month level: category spikes (robust z on monthly totals vs the trailing months) and trends.
Amounts below the currency's `min_anomaly_amount` (currencies.yml) are ignored by design.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from finpilot.insights.frame import Frame, cross_rate, robust_z
from finpilot.insights.models import Finding, Severity
from finpilot.recurring.detect import monthly_equivalent

PERIOD_NOUN = {
    "weekly": "week",
    "biweekly": "two weeks",
    "monthly": "month",
    "quarterly": "quarter",
    "yearly": "year",
}
PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
LOG_MAD_FLOOR = 0.10  # ≈ ±10% typical variation, so fixed-price merchants don't give infinite z


def _severity(excess: float, floor: float) -> Severity:
    if excess >= 10 * floor:
        return "high"
    if excess >= 2 * floor:
        return "medium"
    return "low"


def _merchant_label(row: pd.Series) -> str:
    return str(row["merchant_norm"] or row.get("raw_description") or "Unknown merchant")


def _in_scan(fr: Frame, d: pd.Timestamp) -> bool:
    return d.date() >= fr.scan_start


def duplicates(fr: Frame) -> list[Finding]:
    cfg = fr.cfg["anomalies"]
    window = int(cfg["duplicate_window_days"])
    ceiling = float(fr.thr.small_purchase_ceiling)
    floor = float(fr.thr.min_anomaly_amount)
    sp = fr.spending()
    sp = sp[sp["recurring_rule_id"].isna() & (sp["merchant_norm"] != "")]
    merchants = {k: g.sort_values("date") for k, g in sp.groupby(["merchant_norm", "currency"])}

    def routine(merchant: str, cur: str, amount: float, cl: list[pd.Series]) -> bool:
        """Fixed-price merchant that often charges within the window (tuition, daycare): an identical
        amount close together is its normal rhythm, not evidence of a double charge."""
        g = merchants[(merchant, cur)]
        others = g[~g["id"].isin([r["id"] for r in cl])]
        if len(others) < 3:
            return False
        gaps = others["date"].diff().dt.days.dropna()
        return bool(
            (others["orig"] == amount).mean() >= 0.5
            and len(gaps)
            and (gaps <= window).mean() >= 0.10
        )

    out: list[Finding] = []
    for (merchant, cur, amount), g in sp.groupby(["merchant_norm", "currency", "orig"], sort=True):
        if len(g) < 2:
            continue
        g = g.sort_values(["date", "id"])
        clusters: list[list[pd.Series]] = [[]]
        for _, row in g.iterrows():
            if clusters[-1] and (row["date"] - clusters[-1][-1]["date"]).days > window:
                clusters.append([])
            clusters[-1].append(row)
        for cl in clusters:
            if len(cl) >= 2 and not routine(merchant, cur, amount, cl):
                out += _dup_finding(fr, cl, merchant, cur, amount, window, ceiling, floor)
    return out


def _dup_finding(
    fr: Frame,
    cl: list[pd.Series],
    merchant: str,
    cur: str,
    amount: float,
    window: int,
    ceiling: float,
    floor: float,
) -> list[Finding]:
    if len(cl) < 2 or not _in_scan(fr, cl[-1]["date"]):
        return []
    each = abs(float(cl[0]["amt"]))
    if each <= ceiling:
        return []  # two identical coffees are a habit, not a billing error
    ids = [str(r["id"]) for r in cl]
    extra = each * (len(cl) - 1)
    return [
        Finding(
            fingerprint=f"duplicate_charge:{','.join(sorted(ids))}",
            kind="anomaly",
            type="duplicate_charge",
            severity="high" if extra >= 10 * floor else "medium" if extra >= floor else "low",
            metric="identical_charges_within_window",
            baseline=Decimal(1),
            observed=Decimal(len(cl)),
            threshold=Decimal(2),
            unit="count",
            currency=fr.currency,
            evidence_txn_ids=ids,
            data={
                "merchant": merchant,
                "amount": fr.money(abs(amount), cur),
                "currency": cur,
                "count": len(cl),
                "span_days": (cl[-1]["date"] - cl[0]["date"]).days,
                "window_days": window,
                "dates": ", ".join(r["date"].strftime("%d %b") for r in cl),
                "extra_reporting": fr.money(extra),
            },
            period_start=cl[0]["date"].date(),
            period_end=cl[-1]["date"].date(),
        )
    ]


def outliers(fr: Frame) -> list[Finding]:
    """Merchant baseline when the merchant has ≥ N other charges, else the category baseline."""
    cfg = fr.cfg["anomalies"]
    zt, min_ratio = float(cfg["robust_z"]), float(cfg["min_ratio"])
    m_min, c_min = int(cfg["merchant_min_history"]), int(cfg["category_min_history"])
    floor = float(fr.thr.min_anomaly_amount)
    sp = fr.spending()
    if sp.empty:
        return []
    logs = np.log(np.abs(sp["amt"].to_numpy(dtype=float)))
    sp = sp.assign(_log=logs)
    by_merchant = {k: g for k, g in sp.groupby("merchant_norm") if k}
    by_category = {k: g for k, g in sp.groupby("category_id") if k}
    # bills that belong to a detected series are judged by the price-hike logic, not by a
    # category baseline that mixes them with other bills
    series_ids = {str(i) for d in fr.detected for i in d["txn_ids_json"]}
    out: list[Finding] = []
    scan = sp[sp["date"] >= pd.Timestamp(fr.scan_start)]
    for _, row in scan.iterrows():
        x = abs(float(row["amt"]))
        if x < floor or row["id"] in series_ids:
            continue
        kind, base = None, None
        g = by_merchant.get(row["merchant_norm"])
        if g is not None and len(g) - 1 >= m_min:
            kind, base = "merchant_outlier", g[g["id"] != row["id"]]
        else:
            own = g[g["id"] != row["id"]] if g is not None else None
            if (
                own is not None
                and len(own) >= 2
                and x < min_ratio * float(np.exp(np.median(own["_log"])))
            ):
                continue  # normal for this merchant, even if large for the category
            g = by_category.get(row["category_id"])
            if g is not None and len(g) - 1 >= c_min:
                kind, base = "category_outlier", g[g["id"] != row["id"]]
        if base is None:
            continue
        z, med_log, _ = robust_z(
            float(row["_log"]), base["_log"].to_numpy(dtype=float), LOG_MAD_FLOOR
        )
        median = math.exp(med_log)
        ratio = x / median
        if z < zt or ratio < min_ratio:
            continue
        out.append(
            Finding(
                fingerprint=f"{kind}:{row['id']}",
                kind="anomaly",
                type=kind,
                severity=_severity(x - median, floor),
                metric="robust_z_log_amount",
                baseline=fr.money(median),
                observed=fr.money(x),
                threshold=Decimal(str(zt)),
                unit="currency",
                currency=fr.currency,
                evidence_txn_ids=[str(row["id"])],
                data={
                    "merchant": _merchant_label(row),
                    "category": row["category_name"],
                    "date": row["date"].strftime("%d %b %Y"),
                    "observed": fr.money(x),
                    "baseline": fr.money(median),
                    "currency": fr.currency,
                    "ratio": round(ratio, 1),
                    "z": round(z, 1),
                    "z_threshold": zt,
                    "min_ratio": min_ratio,
                    "history_n": len(base),
                },
                period_start=row["date"].date(),
                period_end=row["date"].date(),
            )
        )
    return out


def first_time_large(fr: Frame) -> list[Finding]:
    cfg = fr.cfg["anomalies"]
    floor = float(fr.thr.min_anomaly_amount)
    sp = fr.spending()
    sp = sp[(sp["nature"] != "fixed") & (sp["merchant_norm"] != "")]
    if len(sp) < 20:
        return []  # no meaningful percentile yet
    pct = float(cfg["first_time_large_percentile"])
    floor_x = float(cfg["first_time_large_floor_x"])
    first_dates = fr.df[fr.df["merchant_norm"] != ""].groupby("merchant_norm")["date"].min()
    threshold = max(
        floor_x * floor, float(np.percentile(np.abs(sp["amt"].to_numpy(dtype=float)), pct))
    )
    out: list[Finding] = []
    scan = sp[sp["date"] >= pd.Timestamp(fr.scan_start)]
    warmup = pd.Timestamp(fr.df["date"].min()) + pd.Timedelta(
        days=int(cfg.get("first_time_warmup_days", 60))
    )
    for _, row in scan.iterrows():
        if first_dates.get(row["merchant_norm"]) != row["date"] or row["date"] < warmup:
            continue  # at the start of history every merchant looks new
        x = abs(float(row["amt"]))
        if x < threshold:
            continue
        if not pd.isna(row["category_id"]):
            prior = sp[(sp["category_id"] == row["category_id"]) & (sp["date"] < row["date"])]
            if (np.abs(prior["amt"].to_numpy(dtype=float)) >= 0.5 * x).any():
                continue  # the user already makes purchases this size in this category
        out.append(
            Finding(
                fingerprint=f"first_time_large:{row['id']}",
                kind="anomaly",
                type="first_time_large",
                severity=_severity(x - threshold, floor) if x >= 2 * threshold else "medium",
                metric="first_merchant_payment_amount",
                baseline=None,
                observed=fr.money(x),
                threshold=fr.money(threshold),
                unit="currency",
                currency=fr.currency,
                evidence_txn_ids=[str(row["id"])],
                data={
                    "merchant": _merchant_label(row),
                    "date": row["date"].strftime("%d %b %Y"),
                    "observed": fr.money(x),
                    "threshold": fr.money(threshold),
                    "currency": fr.currency,
                    "floor_x": int(floor_x),
                    "min_anomaly": fr.money(floor),
                    "percentile": int(pct),
                },
                period_start=row["date"].date(),
                period_end=row["date"].date(),
            )
        )
    return out


def price_hikes(fr: Frame) -> list[Finding]:
    cfg = fr.cfg["anomalies"]
    floor = float(fr.thr.min_anomaly_amount)
    out: list[Finding] = []
    for d in fr.detected:
        if d["kind"] != "expense":
            continue
        ids = [str(i) for i in d["txn_ids_json"]]
        rate = cross_rate(fr, ids)
        for h in d["price_history_json"][1:]:
            pct = float(h.get("pct", 0))
            day = dt.date.fromisoformat(h["date"])
            if pct < float(cfg["price_hike_min_pct"]) or day < fr.scan_start or day > fr.as_of:
                continue
            new, old = Decimal(h["amount"]), Decimal(h["from"])
            annual = float(new - old) * PER_YEAR[d["period"]] * rate
            out.append(
                Finding(
                    fingerprint=f"price_hike:{d['series_key']}:{h['date']}",
                    kind="anomaly",
                    type="price_hike",
                    severity="medium" if annual >= floor else "low",
                    metric="recurring_charge_change_pct",
                    baseline=old,
                    observed=new,
                    threshold=Decimal(str(cfg["price_hike_min_pct"])),
                    unit="pct",
                    currency=d["currency"],
                    evidence_txn_ids=[h["txn_id"]],
                    data={
                        "merchant": d["merchant_norm"],
                        "baseline": old,
                        "observed": new,
                        "orig_currency": d["currency"],
                        "currency": fr.currency,
                        "pct": round(pct, 1),
                        "threshold_pct": cfg["price_hike_min_pct"],
                        "date": day.strftime("%d %b %Y"),
                        "period_noun": PERIOD_NOUN[d["period"]],
                        "annual_extra": fr.money(annual),
                        "detected_id": str(d["id"]),
                    },
                    period_start=day,
                    period_end=day,
                )
            )
    return out


def _nn(v: Any) -> Any:
    """pandas groups None as NaN; turn it back into None."""
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _month_label(m: dt.date) -> str:
    return m.strftime("%B %Y")


def category_spikes(fr: Frame) -> list[Finding]:
    cfg = fr.cfg["anomalies"]
    floor = float(fr.thr.min_anomaly_amount)
    zt = float(cfg["robust_z"])
    k, kmin = int(cfg["category_spike_months"]), int(cfg["category_spike_min_months"])
    out: list[Finding] = []
    if fr.monthly.empty:
        return out
    scan_month = fr.scan_start.replace(day=1)
    for cat, g in fr.monthly.groupby("category_id", dropna=False, sort=True):
        cat = _nn(cat)
        if cat is None:
            continue  # "Uncategorized" swings track categorizer coverage, not behavior
        g = g.sort_values("month")
        vals = g["spend"].to_numpy(dtype=float)
        months = list(g["month"])
        for i, m in enumerate(months):
            if m < scan_month or i < kmin:
                continue
            base = vals[max(0, i - k) : i]
            x = vals[i]
            med = float(np.median(base))
            if med <= 0:
                continue  # first spend in a category is covered by first_time_large / outliers
            z, _, _ = robust_z(x, base, max(0.1 * med, floor / 2))
            if z < zt or x - med < floor:
                continue
            name = g["name"].iloc[0]
            partial = m == fr.as_of.replace(day=1)
            out.append(
                Finding(
                    fingerprint=f"category_spike:{cat}:{m.isoformat()}",
                    kind="anomaly",
                    type="category_spike",
                    severity=_severity(x - med, floor),
                    metric="robust_z_monthly_category_spend",
                    baseline=fr.money(med),
                    observed=fr.money(x),
                    threshold=Decimal(str(zt)),
                    unit="currency",
                    currency=fr.currency,
                    evidence_txn_ids=_month_txns(fr, cat, m),
                    data={
                        "category": name,
                        "category_id": cat,
                        "month_label": _month_label(m),
                        "partial": partial,
                        "observed": fr.money(x),
                        "baseline": fr.money(med),
                        "excess": fr.money(x - med),
                        "currency": fr.currency,
                        "z": round(z, 1),
                        "z_threshold": zt,
                        "baseline_months": len(base),
                    },
                    period_start=m,
                    period_end=min(_month_end(m), fr.as_of),
                )
            )
    return out


def _month_end(m: dt.date) -> dt.date:
    return (pd.Timestamp(m) + pd.offsets.MonthEnd(0)).date()


def _month_txns(fr: Frame, cat: Any, m: dt.date, n: int = 10) -> list[str]:
    sp = fr.spending()
    sel = sp[
        (sp["date"] >= pd.Timestamp(m))
        & (sp["date"] <= pd.Timestamp(_month_end(m)))
        & (sp["category_id"].isna() if cat is None else sp["category_id"] == cat)
    ]
    return [str(i) for i in sel.sort_values("amt")["id"].head(n)]


def category_trends(fr: Frame) -> tuple[list[Finding], list[dict[str, Any]]]:
    """MoM / trailing-3-month deltas for the last full month (+ month-to-date) per category."""
    pcfg = fr.cfg["patterns"]
    floor = float(fr.thr.min_anomaly_amount)
    trends: list[dict[str, Any]] = []
    out: list[Finding] = []
    if fr.monthly.empty:
        return out, trends
    this_month = fr.as_of.replace(day=1)
    for cat, g in fr.monthly.groupby("category_id", dropna=False, sort=True):
        cat = _nn(cat)
        g = g.sort_values("month")
        full = g[g["month"] < this_month]
        mtd = float(g[g["month"] == this_month]["spend"].sum())
        if full.empty:
            continue
        vals = full["spend"].to_numpy(dtype=float)
        last = vals[-1]
        prev = vals[-2] if len(vals) >= 2 else None
        t3 = vals[-4:-1] if len(vals) >= 4 else None
        t3avg = float(np.mean(t3)) if t3 is not None else None
        mom = round((last - prev) / prev * 100, 1) if prev else None
        vs3 = round((last - t3avg) / t3avg * 100, 1) if t3avg else None
        name = g["name"].iloc[0]
        trends.append(
            {
                "category_id": cat,
                "name": name,
                "nature": _nn(g["nature"].iloc[0]),
                "months": [
                    {"month": m.isoformat(), "spend": fr.money(v)}
                    for m, v in zip(g["month"], g["spend"], strict=True)
                ][-12:],
                "last_full_month": fr.money(last),
                "previous_month": fr.money(prev or 0),
                "trailing3_avg": fr.money(t3avg or 0),
                "mom_pct": mom,
                "vs_trailing3_pct": vs3,
                "month_to_date": fr.money(mtd),
            }
        )
        m = full["month"].iloc[-1]
        if (
            cat is not None
            and vs3 is not None
            and t3avg
            and vs3 >= float(pcfg["trend_mom_pct"])
            and last - t3avg >= floor
            and m >= fr.scan_start.replace(day=1) - dt.timedelta(days=31)
        ):
            out.append(
                Finding(
                    fingerprint=f"category_trend_up:{cat}:{m.isoformat()}",
                    kind="trend",
                    type="category_trend_up",
                    severity="low",
                    metric="vs_trailing3_pct",
                    baseline=fr.money(t3avg),
                    observed=fr.money(last),
                    threshold=Decimal(str(pcfg["trend_mom_pct"])),
                    unit="currency",
                    currency=fr.currency,
                    evidence_txn_ids=_month_txns(fr, cat, m),
                    data={
                        "category": name,
                        "category_id": cat,
                        "month_label": _month_label(m),
                        "observed": fr.money(last),
                        "baseline": fr.money(t3avg),
                        "currency": fr.currency,
                        "pct": vs3,
                        "threshold_pct": pcfg["trend_mom_pct"],
                    },
                    period_start=m,
                    period_end=_month_end(m),
                )
            )
    trends.sort(key=lambda t: -float(t["last_full_month"]))
    return out, trends


def run(fr: Frame) -> list[Finding]:
    txn_level: dict[str, Finding] = {}
    priority = {"first_time_large": 0, "merchant_outlier": 1, "category_outlier": 2}
    for f in first_time_large(fr) + outliers(fr):
        key = f.evidence_txn_ids[0]
        if key not in txn_level or priority[f.type] < priority[txn_level[key].type]:
            txn_level[key] = f
    return duplicates(fr) + list(txn_level.values()) + price_hikes(fr) + category_spikes(fr)


__all__ = ["PERIOD_NOUN", "PER_YEAR", "category_trends", "monthly_equivalent", "run"]
