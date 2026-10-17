"""Recurring / subscription detection suite + materializer idempotency (deterministic).

Detection: per persona, load the history months (ground-truth categories), run detection as of
the last history day and match each detected series to the truth series holding the majority of
its transactions. Eligible truth = ≥ 3 history charges (≥ 2 for yearly). Reported: precision,
recall, period accuracy, amount error (vs the series' nominal price after any injected hike, so
variable bills are judged against their true level, not their noisy last charges) and next-date
error (vs the first charge of that series in the held-out months; bank debits due on weekends
post on Monday in the generator).

Materializer: per persona, three INR rules (monthly on the 31st, biweekly, quarterly) start 120
days in the past. Ticks at as_of−60 (catch-up after creation), again the same day (idempotency),
at as_of (catch-up after "downtime") and again. Expected dates come from an independent
enumerator here, not from finpilot/recurring/rules.py.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
import statistics
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from eval import loader
from eval.metrics import prf
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import generate
from eval.synth.personas import PERSONAS, Persona
from finpilot.db.session import user_session
from finpilot.recurring import detect
from finpilot.recurring import service as rec
from finpilot.recurring.materializer import materialize_user

log = logging.getLogger(__name__)
SUITE = "recurring"


def _expected_dates(kind: str, start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    if kind == "biweekly":
        d = start
        while d <= end:
            out.append(d)
            d += dt.timedelta(days=14)
        return out
    step = {"monthly31": 1, "quarterly": 3}[kind]
    y, m, i = start.year, start.month, 0
    while True:
        mm = m - 1 + i * step
        yy, mo = y + mm // 12, mm % 12 + 1
        day = 31 if kind == "monthly31" else start.day
        d = dt.date(yy, mo, min(day, calendar.monthrange(yy, mo)[1]))
        if d > end:
            return out
        if d >= start:
            out.append(d)
        i += 1


def _nominal(p: Persona, key: str, on: dt.date) -> Decimal:
    """The series' true current price: nominal amount × hike multiplier once the hike applies."""
    s = next(x for x in p.series if x.key == key)
    month_idx = (on.year - p.start.year) * 12 + on.month - p.start.month
    mult = s.hike[1] if s.hike and month_idx >= s.hike[0] else Decimal(1)
    return (s.amount * mult).quantize(Decimal("0.01"))


def _materializer_checks(uid: Any, as_of: dt.date) -> list[dict[str, Any]]:
    start = as_of - dt.timedelta(days=120)
    specs = {
        "monthly31": {"frequency": "monthly", "day_of_month": 31},
        "biweekly": {"frequency": "biweekly", "weekday": start.weekday()},
        "quarterly": {"frequency": "quarterly", "day_of_month": start.day},
    }
    rules = {}
    with user_session(uid) as s:
        for k, spec in specs.items():
            r = rec.create_rule(
                s,
                uid,
                {
                    "kind": "expense",
                    "description": f"EVAL RULE {k}",
                    "amount": Decimal("1000"),
                    "currency": "INR",
                    "start_date": start,
                    **spec,
                },
            )
            rules[k] = r["id"]
    t1 = as_of - dt.timedelta(days=60)
    created = [
        materialize_user(uid, None, today=t1).created,
        materialize_user(uid, None, today=t1).created,
        materialize_user(uid, None, today=as_of).created,
        materialize_user(uid, None, today=as_of).created,
    ]
    out = []
    with user_session(uid) as s:
        for k, rid in rules.items():
            dates = [
                r[0]
                for r in s.execute(
                    text(
                        "SELECT date FROM transactions WHERE user_id = :u AND recurring_rule_id = :r ORDER BY date"
                    ),
                    {"u": uid, "r": rid},
                )
            ]
            exp = _expected_dates(k, start, as_of)
            out.append(
                {
                    "rule": k,
                    "expected": [d.isoformat() for d in exp],
                    "actual": [d.isoformat() for d in dates],
                    "correct": dates == exp,
                    "duplicates": len(dates) - len(set(dates)),
                }
            )
    out.append(
        {
            "rule": "_ticks",
            "created_per_tick": created,
            "idempotent": created[1] == 0 and created[3] == 0,
        }
    )
    return out


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas]
    tp = fp = fn = 0
    period_ok: list[bool] = []
    amount_err: list[float] = []
    next_err: list[int] = []
    per_persona: list[dict[str, Any]] = []
    mat_ok: list[bool] = []
    mat_idem: list[bool] = []
    mat_rows: list[dict[str, Any]] = []
    by_period: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in personas:
        ds = generate(p)
        ld = loader.load(ds, ctx.run_tag)
        loader.apply_truth_categories(ds, ld)
        hist = ds.history()
        as_of = max(t.date for t in hist)
        syn_of = {str(v): k for k, v in ld.txn_ids.items()}
        series_of = {t.id: t.series_id for t in ds.txns if t.series_id}
        with user_session(ld.user_id) as s:
            detect.refresh(s, ld.user_id, as_of)
            rows = [
                dict(r)
                for r in s.execute(
                    text(
                        "SELECT * FROM detected_recurrences WHERE user_id = :u AND status <> 'gone'"
                    ),
                    {"u": ld.user_id},
                ).mappings()
            ]
        truth = {s_.id: s_ for s_ in ds.series}
        hist_by_series: dict[str, list] = defaultdict(list)
        hold_by_series: dict[str, list] = defaultdict(list)
        for t in ds.txns:
            if t.series_id:
                (hist_by_series if t.split == "history" else hold_by_series)[t.series_id].append(t)
        eligible = {
            sid
            for sid, s_ in truth.items()
            if len(hist_by_series[sid]) >= (2 if s_.period == "yearly" else 3)
        }
        matched: dict[str, dict] = {}
        p_tp = p_fp = 0
        for r in rows:
            votes: dict[str, int] = defaultdict(int)
            for tid in r["txn_ids_json"]:
                sid = series_of.get(syn_of.get(str(tid), ""))
                if sid:
                    votes[sid] += 1
            best = max(votes.items(), key=lambda kv: kv[1], default=(None, 0))
            sid = best[0] if best[1] * 2 >= len(r["txn_ids_json"]) else None
            if sid is None or sid in matched:
                fp += 1
                p_fp += 1
                res.cases.append(
                    Case(
                        case_id=f"{p.name}:fp:{r['series_key']}",
                        input={
                            "persona": p.name,
                            "merchant": r["merchant_norm"],
                            "n": r["n_occurrences"],
                        },
                        expected={"recurring": False},
                        actual={
                            "period": r["period"],
                            "amount": str(r["amount_typical"]),
                            "currency": r["currency"],
                        },
                        passed=False,
                        score=0.0,
                        error_type="false_positive",
                    )
                )
                continue
            matched[sid] = r
        for sid in sorted(eligible):
            s_ = truth[sid]
            hit = matched.get(sid)
            by_period[s_.period]["truth"] += 1
            if hit is None:
                fn += 1
                res.cases.append(
                    Case(
                        case_id=sid,
                        input={
                            "persona": p.name,
                            "series": s_.key,
                            "period": s_.period,
                            "n_history": len(hist_by_series[sid]),
                        },
                        expected={"period": s_.period, "amount": str(s_.amount)},
                        actual={"detected": False},
                        passed=False,
                        score=0.0,
                        error_type="missed",
                    )
                )
                continue
            tp += 1
            p_tp += 1
            by_period[s_.period]["detected"] += 1
            true_amt = _nominal(p, s_.key, max(t.date for t in hist_by_series[sid]))
            err = abs(float(hit["amount_typical"]) - float(true_amt)) / float(true_amt)
            amount_err.append(err)
            ok_period = hit["period"] == s_.period
            period_ok.append(ok_period)
            hold = sorted(hold_by_series[sid], key=lambda t: t.date)
            nd = abs((hit["next_expected"] - hold[0].date).days) if hold else None
            if nd is not None:
                next_err.append(nd)
            res.cases.append(
                Case(
                    case_id=sid,
                    input={
                        "persona": p.name,
                        "series": s_.key,
                        "n_history": len(hist_by_series[sid]),
                    },
                    expected={
                        "period": s_.period,
                        "amount": str(true_amt),
                        "next": hold[0].date.isoformat() if hold else None,
                    },
                    actual={
                        "period": hit["period"],
                        "amount": str(hit["amount_typical"]),
                        "next": hit["next_expected"].isoformat(),
                        "status": hit["status"],
                    },
                    passed=ok_period and err <= 0.05 and (nd is None or nd <= 3),
                    score=1.0 - min(err, 1.0),
                    error_type=None if ok_period else "wrong_period",
                )
            )
        # truth series outside eligibility that were still matched are not counted either way
        per_persona.append(
            {
                "persona": p.name,
                "currency": p.reporting_currency,
                **prf(p_tp, p_fp, len(eligible) - p_tp),
                "eligible": len(eligible),
                "detected": len(rows),
            }
        )
        checks = _materializer_checks(ld.user_id, as_of)
        mat_rows += [{"persona": p.name, **c} for c in checks]
        mat_ok += [c["correct"] and c["duplicates"] == 0 for c in checks if c["rule"] != "_ticks"]
        mat_idem += [c["idempotent"] for c in checks if c["rule"] == "_ticks"]
        loader.cleanup(ctx.run_tag)

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    o = prf(tp, fp, fn)
    for k in ("precision", "recall", "f1"):
        add(k, o[k], "ratio", scope="overall", primary=True)
    add(
        "period_accuracy",
        sum(period_ok) / max(len(period_ok), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "amount_mape",
        statistics.mean(amount_err) if amount_err else 0.0,
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "next_date_mae_days",
        statistics.mean(next_err) if next_err else 0.0,
        "days",
        scope="overall",
        primary=True,
    )
    add(
        "next_date_within_3d",
        sum(e <= 3 for e in next_err) / max(len(next_err), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "materializer_correct",
        sum(mat_ok) / max(len(mat_ok), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "materializer_idempotent",
        sum(mat_idem) / max(len(mat_idem), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    for period, b in sorted(by_period.items()):
        add("recall", b["detected"] / max(b["truth"], 1), "ratio", scope="period", period=period)
    for row in per_persona:
        add("f1", row["f1"], "ratio", scope="persona", persona=row["persona"])
    res.details = {
        "per_persona": per_persona,
        "per_period": {k: dict(v) for k, v in by_period.items()},
        "next_date_errors": sorted(next_err),
        "materializer": mat_rows,
    }
    return res
