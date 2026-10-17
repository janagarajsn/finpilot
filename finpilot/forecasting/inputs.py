"""Builds ForecastInputs for one user from Postgres (declared rules, detected series, history).

Scheduled items: active declared rules (schedule math) first, then active, undismissed detected
series NOT linked to a rule (declared wins). Variable streams: every other transaction in the
lookback window, one stream per category (uncategorized split by direction). Transactions of
active or lapsed series and rule-materialized rows are excluded from streams (they are scheduled,
or they stopped); a series the user dismissed ("not a repeat") stays variable.

FX: cross rates come from stored `fx_rates` (scope 'synthetic' for eval users), latest on or
before as_of; no network call happens while forecasting. The rate used is reported back.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories.rules import thresholds
from finpilot.forecasting.model import ForecastInputs, ScheduledItem, Streams, stable_hash
from finpilot.insights.dashboard import balance
from finpilot.insights.frame import Frame, load_frame
from finpilot.money import get_currency_registry
from finpilot.recurring import detect
from finpilot.recurring.rules import _add_months, occurrences_between
from finpilot.recurring.service import local_today, schedule_of

MONTH_STEPS = {"monthly": 1, "quarterly": 3, "yearly": 12}
DAY_STEPS = {"weekly": 7, "biweekly": 14}


class ForecastError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Rate:
    rate: Decimal  # reporting units per 1 unit of `currency`
    date: dt.date | None
    source: str  # same | stored | history | unavailable

    def as_dict(self, currency: str, reporting: str) -> dict[str, Any]:
        return {
            "currency": currency,
            "reporting_currency": reporting,
            "rate": str(self.rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)),
            "rate_date": self.date.isoformat() if self.date else None,
            "source": self.source,
        }


@dataclass
class Context:
    """Everything scenario application, goals and the UI need besides the engine inputs."""

    user_id: uuid.UUID
    currency: str
    data_version: int
    is_synthetic: bool
    months_of_data: float
    frame: Frame
    detected: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    streams_by_category: dict[str, str]  # category id / lower name → stream key
    daily_net: dict[dt.date, float]  # history (all transactions) for naive baselines
    median_monthly_income: float
    rates: dict[str, Rate] = field(default_factory=dict)
    # one-off moves into savings/investments over the lookback (per month, positive): not
    # resampled into the cash forecast, reported so the UI can say so
    unprojected_savings_monthly: float = 0.0
    approximate: bool = False

    def rate(self, s: Session, currency: str) -> Rate:
        currency = currency.upper()
        if currency not in self.rates:
            self.rates[currency] = cross_rate(
                s, currency, self.currency, self.frame.as_of, self.is_synthetic, self.frame
            )
        return self.rates[currency]


def cross_rate(
    s: Session,
    currency: str,
    reporting: str,
    as_of: dt.date,
    synthetic: bool,
    fr: Frame | None = None,
) -> Rate:
    if currency == reporting:
        return Rate(Decimal(1), None, "same")
    scope = "synthetic" if synthetic else "real"
    base = get_currency_registry().base

    def inr_per(c: str) -> tuple[Decimal, dt.date] | None:
        if c == base:
            return Decimal(1), as_of
        row = s.execute(
            text("""SELECT rate_inr_per_unit, rate_date FROM fx_rates WHERE scope = :sc AND quote_currency = :c
                    AND rate_date <= :d ORDER BY rate_date DESC LIMIT 1"""),
            {"sc": scope, "c": c, "d": as_of},
        ).first()
        return (Decimal(row[0]), row[1]) if row else None

    a, b = inr_per(currency), inr_per(reporting)
    if a and b:
        return Rate(a[0] / b[0], min(a[1], b[1]), "stored")
    if fr is not None and not fr.df.empty:  # last resort: the user's own latest converted charge
        rows = fr.df[fr.df["currency"] == currency]
        if len(rows):
            last = rows.sort_values("date").iloc[-1]
            return Rate(
                Decimal(str(abs(last["amt"] / last["orig"]))), last["date"].date(), "history"
            )
    return Rate(Decimal(0), None, "unavailable")


def _fx_sigma(
    s: Session, currency: str, reporting: str, as_of: dt.date, days: int, synthetic: bool
) -> float:
    """Daily σ of log(cross rate) from stored rates (calendar-day scaled)."""
    scope = "synthetic" if synthetic else "real"
    base = get_currency_registry().base

    def series(c: str) -> pd.Series | None:
        if c == base:
            return None
        rows = s.execute(
            text("""SELECT rate_date, rate_inr_per_unit FROM fx_rates WHERE scope = :sc AND quote_currency = :c
                    AND rate_date BETWEEN :a AND :b ORDER BY rate_date"""),
            {"sc": scope, "c": c, "a": as_of - dt.timedelta(days=days), "b": as_of},
        ).all()
        return pd.Series({r[0]: float(r[1]) for r in rows}) if rows else pd.Series(dtype=float)

    num, den = series(currency), series(reporting)
    if num is not None and num.empty:
        return 0.0
    log = np.log(num) if num is not None else None
    if den is not None:
        if den.empty:
            return 0.0
        log = (log - np.log(den)).dropna() if log is not None else -np.log(den)
    if log is None or len(log) < 10:
        return 0.0
    rets = np.diff(log.to_numpy())
    span = (log.index[-1] - log.index[0]).days or 1
    return float(np.std(rets) * math.sqrt(len(rets) / span))


def _posts_on_business_days(df: pd.DataFrame, d: dict[str, Any]) -> bool:
    """A bank debit/credit that has never posted on a weekend (≥ 4 charges) is assumed to move
    to Monday when due on a weekend; card subscriptions post any day and keep their date."""
    ids = {str(i) for i in d["txn_ids_json"]}
    if len(ids) < 4 or df.empty:
        return False
    days = df[df["id"].isin(ids)]["date"].dt.dayofweek
    return len(days) >= 4 and bool((days < 5).all())


def _q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def build(
    s: Session,
    user_id: uuid.UUID,
    as_of: dt.date | None = None,
    horizon: int | None = None,
    fx: Any = None,
    refresh_detection: bool = True,
    cfg: dict[str, Any] | None = None,
    seed: int | None = None,
) -> tuple[ForecastInputs, Context]:
    """`seed` defaults to hash(user, as_of); evals pass a persona-based seed (users are re-created)."""
    cfg = cfg or thresholds()
    fc = cfg["forecast"]
    user = (
        s.execute(
            text("""SELECT u.reporting_currency, u.timezone, u.data_version, u.is_synthetic, st.cash_buffer
                    FROM users u LEFT JOIN user_settings st ON st.user_id = u.id WHERE u.id = :u"""),
            {"u": user_id},
        )
        .mappings()
        .one()
    )
    cur = user["reporting_currency"]
    as_of = as_of or local_today(user["timezone"])
    horizon = int(horizon or fc["horizon_days"])
    if not 7 <= horizon <= 366:
        raise ForecastError("Horizon must be between 7 and 366 days.")
    if refresh_detection:
        detect.refresh(s, user_id, as_of)
    fr = load_frame(s, user_id, as_of, cur, cfg, lookback_days=None)
    start, approx = balance(s, user_id, cur, fx, as_of)
    buffer = (
        user["cash_buffer"]
        if user["cash_buffer"] is not None
        else get_currency_registry().get(cur).thresholds.default_cash_buffer
    )
    first = fr.history_start
    months = 0.0 if first is None else round(((as_of - first).days + 1) / 30.44, 2)
    df = fr.df
    rules = [
        dict(r)
        for r in s.execute(
            text(
                "SELECT * FROM recurring_rules WHERE user_id = :u AND status = 'active' ORDER BY created_at"
            ),
            {"u": user_id},
        ).mappings()
    ]
    ctx = Context(
        user_id=user_id,
        currency=cur,
        data_version=user["data_version"],
        is_synthetic=user["is_synthetic"],
        months_of_data=months,
        frame=fr,
        detected=fr.detected,
        rules=rules,
        streams_by_category={},
        daily_net={},
        median_monthly_income=0.0,
        approximate=approx,
    )

    # ------------------------------------------------------------------ history aggregates
    if not df.empty:
        dn = df.groupby(df["date"].dt.date)["amt"].sum()
        ctx.daily_net = {d: float(v) for d, v in dn.items()}
        inc = df[(df["amt"] > 0) & (df["flow_type"] == "income")]
        recent = inc[inc["date"] >= pd.Timestamp(as_of - dt.timedelta(days=183))]
        monthly = recent.groupby(recent["date"].dt.to_period("M"))["amt"].sum()
        ctx.median_monthly_income = float(monthly.median()) if len(monthly) else 0.0

    # ------------------------------------------------------------------ variable streams
    excluded: set[str] = set()
    for d in fr.detected:
        if d["status"] == "lapsed" or (d["status"] == "active" and not d["dismissed"]):
            excluded |= {str(i) for i in d["txn_ids_json"]}
    L = 0 if first is None else min(int(fc["lookback_days"]), (as_of - first).days + 1)
    streams = Streams([], np.zeros((0, 0)), [], [], [])
    if L >= 14:
        win_start = as_of - dt.timedelta(days=L - 1)
        unscheduled = df[
            (df["date"] >= pd.Timestamp(win_start))
            & ~df["id"].isin(excluded)
            & df["recurring_rule_id"].isna()
        ]
        # Unscheduled moves into savings/investments (an FD, a lump-sum fund purchase) are the
        # user's own decision, not a habit to resample: bootstrapping them projected a ₹10 L FD
        # as a recurring monthly drain. Scheduled ones (SIP, EPF: rules or detected series)
        # stay in the forecast as scheduled items.
        to_savings = unscheduled["flow_type"] == "savings"
        ctx.unprojected_savings_monthly = float(
            -unscheduled.loc[to_savings & (unscheduled["amt"] < 0), "amt"].sum() / L * 30.44
        )
        v = unscheduled[~to_savings].copy()
        v["key"] = [
            c if not pd.isna(c) else ("uncat:in" if a > 0 else "uncat:out")
            for c, a in zip(v["category_id"], v["amt"], strict=True)
        ]
        hist_dates = [win_start + dt.timedelta(days=i) for i in range(L)]
        keys = sorted(v["key"].unique()) if len(v) else []
        pivot = (
            v.assign(day=v["date"].dt.date).pivot_table(
                index="key", columns="day", values="amt", aggfunc="sum", fill_value=0.0
            )
            if len(v)
            else pd.DataFrame()
        )
        mat = np.zeros((len(keys), L))
        for i, k in enumerate(keys):
            row = pivot.loc[k]
            for d, val in row.items():
                mat[i, (d - win_start).days] = float(val)
        meta = v.groupby("key").agg(
            label=("category_name", "first"),
            nature=("nature", "first"),
            cat=("category_id", "first"),
        )
        labels = [
            str(meta.loc[k, "label"])
            if not k.startswith("uncat")
            else ("Uncategorized income" if k == "uncat:in" else "Uncategorized")
            for k in keys
        ]
        natures = [str(meta.loc[k, "nature"]) for k in keys]
        streams = Streams(hist_dates, mat, keys, labels, natures)
        for k, lab in zip(keys, labels, strict=True):
            ctx.streams_by_category[k] = k
            ctx.streams_by_category[lab.lower()] = k

    # ------------------------------------------------------------------ scheduled items
    end = as_of + dt.timedelta(days=horizon)
    tomorrow = as_of + dt.timedelta(days=1)
    items: list[ScheduledItem] = []
    fx_currencies: set[str] = set()
    cv_by_rule = {
        str(d["promoted_to_rule_id"]): float(d["amount_cv"])
        for d in fr.detected
        if d["promoted_to_rule_id"]
    }
    for r in rules:
        rate = ctx.rate(s, r["currency"])
        if rate.source == "unavailable":
            ctx.approximate = True
            continue
        first_due = r["next_run_date"] or tomorrow
        sign = -1 if r["kind"] == "expense" else 1
        amt = float(_q2(Decimal(r["amount"]) * rate.rate)) * sign
        for due in occurrences_between(schedule_of(r), min(first_due, tomorrow), end):
            items.append(
                ScheduledItem(
                    key=f"rule:{r['id']}:{due.isoformat()}",
                    date=max(due, tomorrow),
                    amount=amt,
                    label=r["description"],
                    source="declared",
                    kind=r["kind"],
                    cv=cv_by_rule.get(str(r["id"]), 0.0),
                    currency=r["currency"] if r["currency"] != cur else None,
                    category_id=str(r["category_id"]) if r["category_id"] else None,
                    ref_id=str(r["id"]),
                )
            )
        if r["currency"] != cur:
            fx_currencies.add(r["currency"])
    for d in fr.detected:
        if d["status"] != "active" or d["dismissed"] or d["promoted_to_rule_id"] is not None:
            continue
        rate = ctx.rate(s, d["currency"])
        if rate.source == "unavailable":
            ctx.approximate = True
            continue
        sign = -1 if d["kind"] == "expense" else 1
        amt = float(_q2(Decimal(d["amount_typical"]) * rate.rate)) * sign
        nxt: dt.date = d["next_expected"]
        business_days = _posts_on_business_days(df, d)
        i = 0
        while True:
            if d["period"] in MONTH_STEPS:
                occ = _add_months(nxt, MONTH_STEPS[d["period"]] * i, nxt.day)
            else:
                occ = nxt + dt.timedelta(days=DAY_STEPS[d["period"]] * i)
            if occ > end:
                break
            posted = (
                occ + dt.timedelta(days=(7 - occ.weekday()) % 7)
                if business_days and occ.weekday() >= 5
                else occ
            )
            if (occ > as_of or i == 0) and posted <= end:  # an overdue charge is expected tomorrow
                items.append(
                    ScheduledItem(
                        key=f"det:{d['series_key']}:{occ.isoformat()}",
                        date=max(posted, tomorrow),
                        amount=amt,
                        label=d["merchant_norm"].title(),
                        source="detected",
                        kind=d["kind"],
                        cv=float(d["amount_cv"]),
                        currency=d["currency"] if d["currency"] != cur else None,
                        category_id=str(d["category_id"]) if d["category_id"] else None,
                        ref_id=str(d["id"]),
                    )
                )
            i += 1
        if d["currency"] != cur:
            fx_currencies.add(d["currency"])

    # ------------------------------------------------------------------ payday effect
    share = float(fc["payday_min_share"])
    threshold = share * ctx.median_monthly_income
    paydays = sorted(
        {
            it.date
            for it in items
            if it.kind == "income" and threshold > 0 and it.amount >= threshold
        }
    )
    mult = 1.0
    if not streams.empty and threshold > 0:
        hist_pay = sorted(
            {
                t.date()
                for t, a, f in zip(df["date"], df["amt"], df["flow_type"], strict=True)
                if f == "income" and a >= threshold
            }
        )
        disc = [i for i, n in enumerate(streams.natures) if n in ("discretionary", "unknown")]
        if len(hist_pay) >= 2 and disc:
            daily = -streams.matrix[disc].sum(axis=0)  # positive = spend
            w = int(fc["payday_window_days"])
            idx = {d: i for i, d in enumerate(streams.hist_dates)}
            windows = [
                daily[idx[p] : idx[p] + w].sum()
                for p in hist_pay
                if p in idx and idx[p] + w <= len(daily)
            ]
            typical = daily.mean() * w
            if len(windows) >= 2 and typical > 0:
                lo, hi = fc["payday_multiplier_clip"]
                mult = float(np.clip(statistics.mean(windows) / typical, lo, hi))

    fx_vol = {
        c: _fx_sigma(s, c, cur, as_of, int(fc["fx_vol_lookback_days"]), user["is_synthetic"])
        for c in sorted(fx_currencies)
    }
    inp = ForecastInputs(
        as_of=as_of,
        horizon=horizon,
        currency=cur,
        start_balance=float(start),
        buffer=float(buffer),
        streams=streams,
        scheduled=sorted(items, key=lambda i: (i.date, i.key)),
        seed=seed if seed is not None else stable_hash(f"{user_id}:{as_of.isoformat()}"),
        n_paths=int(fc["n_paths"]),
        half_life=float(fc["half_life_days"]),
        paydays=paydays,
        payday_window=int(fc["payday_window_days"]),
        payday_multiplier=mult,
        fx_vol=fx_vol,
        low_confidence=months < float(fc["sparse_months"]),
        band_widen=float(fc["sparse_band_widen"]),
    )
    return inp, ctx
