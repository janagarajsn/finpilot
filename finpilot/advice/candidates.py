"""Deterministic advice candidates (the LLM only picks among these and words them).

Every candidate has: a stable key (dismissals are remembered by key), the finding ids it rests
on (≥ 1), evidence transaction ids, the named entities its text must mention (merchant, category,
goal, date), est_monthly_impact / est_total_impact in the reporting currency, and a SIMULATED
effect: the scenario re-run with the forecaster's seed (common random numbers) gives the change
in expected end balance, bad-case low and chance of dipping below the cushion.

The advisor also owns three finding types (kinds `forecast` / `goal`): forecast_shortfall,
goal_gap and planned_purchase_risk, so every candidate can cite something concrete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.forecasting import scenarios
from finpilot.forecasting.inputs import Context
from finpilot.forecasting.model import Bands, ForecastInputs, simulate
from finpilot.forecasting.service import ForecastResult
from finpilot.forecasting.simulator import Outcome, outcome
from finpilot.insights.models import AnalysisReport, Finding
from finpilot.insights.text import render

SEV_BONUS = {"high": 0.3, "medium": 0.2, "low": 0.1, "info": 0.0}
CUT_STEPS = (10, 20, 30, 40, 50)
DISMISS_MEMORY_DAYS = 90


def q(x: float | Decimal) -> Decimal:
    return Decimal(str(round(float(x), 2))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


@dataclass
class Candidate:
    key: str
    kind: str
    trigger_kind: str  # anomaly | subscription | forecast_shortfall | goal_gap | pattern
    entities: dict[str, str]
    finding_ids: list[str]
    evidence_txn_ids: list[str]
    est_monthly_impact: Decimal
    est_total_impact: Decimal
    facts: dict[str, Any]
    scenario: dict[str, Any] | None = None
    simulation: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def public(self, cid: str) -> dict[str, Any]:
        """What the LLM sees: ids, entities and computed facts only."""
        return {
            "id": cid,
            "kind": self.kind,
            "trigger": self.trigger_kind,
            "names": self.entities,
            "finding_ids": self.finding_ids,
            "facts": self.facts,
        }


@dataclass
class AdviceInputs:
    s: Session
    user_id: uuid.UUID
    currency: str
    report: AnalysisReport
    forecast: ForecastResult
    inp: ForecastInputs
    ctx: Context
    base: Bands
    base_outcome: Outcome
    monthly_spend: float
    dismissed: dict[str, str]  # candidate key → reason


# ---------------------------------------------------------------------------- advisor findings
def advisor_findings(a: AdviceInputs, recent_scenarios: list[dict[str, Any]]) -> list[Finding]:
    cur, o, fc = a.currency, a.base_outcome, a.forecast
    out: list[Finding] = []
    if o.verdict != "affordable":
        out.append(
            Finding(
                fingerprint=f"forecast_shortfall:{fc.as_of.isoformat()}",
                kind="forecast",
                type="forecast_shortfall",
                severity="high" if o.verdict == "not_affordable" else "medium",
                metric="p10_min_balance_vs_cushion",
                baseline=fc.buffer,
                observed=o.min_p10,
                threshold=fc.buffer,
                unit="currency",
                currency=cur,
                data={
                    "min_p10": o.min_p10,
                    "date": o.min_p10_date.isoformat(),
                    "buffer": fc.buffer,
                    "currency": cur,
                    "p_below_buffer": round(o.p_below_buffer * 100),
                    "horizon": fc.horizon,
                    "forecast_run_id": fc.id,
                },
                period_start=fc.as_of,
                period_end=o.min_p10_date,
            )
        )
    for g in fc.goals:
        if g["status"] not in ("behind", "not_on_track", "over"):
            continue
        data = {**g, "currency": cur}
        out.append(
            Finding(
                fingerprint=f"goal_gap:{g['id']}:{fc.as_of:%Y-%m}",
                kind="goal",
                type="goal_gap",
                severity="medium",
                metric="goal_status",
                unit="currency",
                currency=cur,
                observed=Decimal(str(g.get("projected") or g.get("monthly_savings") or 0)),
                threshold=Decimal(str(g.get("cap") or g.get("required_monthly") or 0)),
                data=data,
            )
        )
    for r in recent_scenarios:
        res = r["result_json"]
        sc = r["scenario_json"] or {}
        if sc.get("type") != "one_time_purchase" or res["result"]["verdict"] == "affordable":
            continue
        out.append(
            Finding(
                fingerprint=f"planned_purchase_risk:{r['id']}",
                kind="forecast",
                type="planned_purchase_risk",
                severity="medium",
                metric="what_if_verdict",
                observed=Decimal(res["result"]["min_p10"]),
                threshold=Decimal(res["buffer"]),
                unit="currency",
                currency=cur,
                data={
                    "label": sc.get("label") or "purchase",
                    "amount": Decimal(str(res["applied"][0]["amount"])),
                    "date": res["applied"][0]["date"],
                    "verdict": res["result"]["verdict"],
                    "currency": cur,
                    "min_p10": Decimal(res["result"]["min_p10"]),
                    "scenario": sc,
                    "levers": res["levers"],
                    "forecast_run_id": str(r["id"]),
                },
            )
        )
        break  # the latest risky what-if only
    for f in out:
        render(f)
    return out


def recent_risky_scenarios(s: Session, user_id: uuid.UUID, days: int = 7) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in s.execute(
            text("""SELECT id, scenario_json, result_json FROM forecast_runs WHERE user_id = :u AND kind = 'scenario'
                AND created_at > now() - make_interval(days => :d) ORDER BY created_at DESC LIMIT 5"""),
            {"u": user_id, "d": days},
        ).mappings()
    ]


def dismissed_memory(s: Session, user_id: uuid.UUID) -> dict[str, str]:
    rows = s.execute(
        text("""SELECT DISTINCT ON (candidate_key) candidate_key, coalesce(dismiss_reason, '') AS reason
                FROM recommendations WHERE user_id = :u AND status = 'dismissed'
                  AND decided_at > now() - make_interval(days => :d)
                ORDER BY candidate_key, decided_at DESC"""),
        {"u": user_id, "d": DISMISS_MEMORY_DAYS},
    ).all()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------- simulation helper
def _simulate(a: AdviceInputs, sc_dict: dict[str, Any]) -> dict[str, Any] | None:
    try:
        sc = scenarios.parse(sc_dict)
        b = simulate(scenarios.Applier(a.s, a.inp, a.ctx).apply(sc))
    except Exception:
        return None
    o = outcome(b, a.inp.buffer)
    base = a.base_outcome
    return {
        "end_change": q(b.mean[-1] - a.base.mean[-1]),
        "min_p10_before": base.min_p10,
        "min_p10_after": o.min_p10,
        "p_below_buffer_before": round(base.p_below_buffer * 100),
        "p_below_buffer_after": round(o.p_below_buffer * 100),
        "verdict_before": base.verdict,
        "verdict_after": o.verdict,
        "horizon_days": a.inp.horizon,
    }


def _score(c: Candidate, a: AdviceInputs, severity: str) -> float:
    spend = max(a.monthly_spend, 1.0)
    impact = float(c.est_monthly_impact) / spend + (
        float(c.est_total_impact) / spend / 6 if c.est_monthly_impact == 0 else 0
    )
    relief = 0.0
    if c.simulation:
        relief = (
            c.simulation["p_below_buffer_before"] - c.simulation["p_below_buffer_after"]
        ) / 100
    return round(impact + 2 * relief + SEV_BONUS.get(severity, 0), 4)


def _finding_index(findings: list[Finding]) -> dict[str, Finding]:
    return {f.id: f for f in findings if f.id}


# ---------------------------------------------------------------------------- builders
def build(a: AdviceInputs, findings: list[Finding]) -> list[Candidate]:
    """All candidates (dismissed keys removed), best first."""
    out: list[Candidate] = []
    by_type: dict[str, list[Finding]] = {}
    for f in findings:
        by_type.setdefault(f.type, []).append(f)

    def add(c: Candidate | None, severity: str = "low") -> None:
        if c is None or not c.finding_ids or c.key in a.dismissed:
            return
        c.score = _score(c, a, severity)
        out.append(c)

    # 1. cancel a flagged subscription
    for sub in a.report.subscriptions:
        if sub.status != "active" or not sub.flags:
            continue
        cites = [
            f
            for f in findings
            if f.kind == "subscription"
            and (
                sub.id in (f.data.get("subscription_ids") or [])
                or f.data.get("subscription_id") == sub.id
            )
        ]
        cites += [f for f in by_type.get("price_hike", []) if f.data.get("detected_id") == sub.id]
        if not cites:
            continue
        sim = _simulate(a, {"type": "cancel_subscription", "subscription_id": sub.id})
        if sim is None:
            continue
        add(
            Candidate(
                key=f"cancel_subscription:{sub.merchant}",
                kind="cancel_subscription",
                trigger_kind="subscription",
                entities={"merchant": sub.merchant},
                finding_ids=[str(f.id) for f in cites],
                evidence_txn_ids=sub.evidence_txn_ids[-3:],
                est_monthly_impact=sub.monthly_cost,
                est_total_impact=sim["end_change"],
                facts={
                    "monthly_cost": sub.monthly_cost,
                    "annual_cost": sub.annual_cost,
                    "charge": sub.amount,
                    "charge_currency": sub.currency,
                    "flags": sub.flags,
                    "overlap_with": sub.overlap_with,
                    "usage_rating": sub.usage_rating,
                    **_rate_facts(a, sub.currency),
                    "simulation": sim,
                },
                scenario={"type": "cancel_subscription", "subscription_id": sub.id},
                simulation=sim,
            ),
            max((f.severity for f in cites), key=lambda x: SEV_BONUS[x]),
        )

    # 2. cap a category that spiked / trends up / dominates (pattern)
    seen_cats: set[str] = set()
    for f in by_type.get("category_spike", []) + by_type.get("category_trend_up", []):
        cat_id, name = f.data.get("category_id"), f.data.get("category")
        key = a.ctx.streams_by_category.get(str(cat_id)) if cat_id else None
        if (
            not key
            or key in seen_cats
            or f.baseline is None
            or f.observed is None
            or f.observed <= f.baseline
        ):
            continue
        seen_cats.add(key)
        excess = f.observed - f.baseline
        pct = min(round(float(excess / f.observed) * 100), 50)
        if pct < 5:
            continue
        sim = _simulate(a, {"type": "category_cut", "category": key, "pct": pct})
        if sim is None:
            continue
        add(
            Candidate(
                key=f"category_cap:{cat_id}",
                kind="category_cap",
                trigger_kind="anomaly" if f.type == "category_spike" else "pattern",
                entities={"category": str(name), "month": str(f.data.get("month_label", ""))},
                finding_ids=[str(f.id)],
                evidence_txn_ids=f.evidence_txn_ids[:5],
                est_monthly_impact=q(excess),
                est_total_impact=sim["end_change"],
                facts={
                    "category": name,
                    "cap": f.baseline,
                    "recent_month": f.observed,
                    "cut_pct": pct,
                    "simulation": sim,
                },
                scenario={"type": "category_cut", "category": key, "pct": pct},
                simulation=sim,
            ),
            f.severity,
        )
    for f in by_type.get("concentration", []) + by_type.get("impulse_proxy", []):
        top = _top_discretionary(a)
        if top is None or top[0] in seen_cats:
            continue
        key, name, monthly = top
        seen_cats.add(key)
        sim = _simulate(a, {"type": "category_cut", "category": key, "pct": 15})
        if sim is None:
            continue
        add(
            Candidate(
                key=f"category_cap:{key}",
                kind="category_cap",
                trigger_kind="pattern",
                entities={"category": name},
                finding_ids=[str(f.id)],
                evidence_txn_ids=f.evidence_txn_ids[:5],
                est_monthly_impact=q(monthly * 0.15),
                est_total_impact=sim["end_change"],
                facts={
                    "category": name,
                    "monthly_spend": q(monthly),
                    "cut_pct": 15,
                    "simulation": sim,
                },
                scenario={"type": "category_cut", "category": key, "pct": 15},
                simulation=sim,
            ),
            f.severity,
        )

    # 3. close a forecast shortfall with the smallest verified cut of the top discretionary category
    for f in by_type.get("forecast_shortfall", []):
        top = _top_discretionary(a)
        if top is None:
            break
        key, name, monthly = top
        best = None
        for pct in CUT_STEPS:
            sim = _simulate(a, {"type": "category_cut", "category": key, "pct": pct})
            if sim is None:
                break
            best = (pct, sim)
            if sim["verdict_after"] == "affordable":
                break
        if best and best[1]["min_p10_after"] > best[1]["min_p10_before"]:
            pct, sim = best
            add(
                Candidate(
                    key=f"close_shortfall:{key}",
                    kind="close_shortfall",
                    trigger_kind="forecast_shortfall",
                    entities={"category": name, "date": f.data["date"]},
                    finding_ids=[str(f.id)],
                    evidence_txn_ids=[],
                    est_monthly_impact=q(monthly * pct / 100),
                    est_total_impact=sim["end_change"],
                    facts={
                        "category": name,
                        "monthly_spend": q(monthly),
                        "cut_pct": pct,
                        "low_date": f.data["date"],
                        "cushion": a.forecast.buffer,
                        "simulation": sim,
                    },
                    scenario={"type": "category_cut", "category": key, "pct": pct},
                    simulation=sim,
                ),
                f.severity,
            )

    # 4. goals: catch up a savings target / respect a spending cap
    for f in by_type.get("goal_ahead", []):
        g = f.data
        surplus = Decimal(str(g["surplus"]))
        sim = _simulate(
            a, {"type": "savings_transfer", "amount": str(surplus), "frequency": "monthly"}
        )
        add(
            Candidate(
                key=f"goal_surplus:{g['id']}",
                kind="goal_surplus",
                trigger_kind="goal_gap",
                entities={"goal": g["name"]},
                finding_ids=[str(f.id)],
                evidence_txn_ids=[],
                est_monthly_impact=Decimal(0),
                est_total_impact=Decimal(0),
                facts={
                    "goal": g["name"],
                    "surplus": surplus,
                    "required_monthly": g["required_monthly"],
                    "monthly_savings": g["monthly_savings"],
                    "eta": g["eta"],
                    "target_date": g.get("target_date"),
                    "simulation": sim or {},
                },
                scenario={
                    "type": "savings_transfer",
                    "amount": str(surplus),
                    "frequency": "monthly",
                },
                simulation=sim or {},
            ),
            f.severity,
        )
    for f in by_type.get("goal_gap", []):
        g = f.data
        if g["kind"] == "debt_payoff":
            extra = Decimal(str(g.get("required_monthly") or 0)) - Decimal(
                str(g.get("monthly_payment") or 0)
            )
            if extra <= 0:
                continue
            extra = q(extra)
            sim = _simulate(
                a,
                {
                    "type": "recurring_cost",
                    "amount": str(extra),
                    "frequency": "monthly",
                    "label": "Extra debt payment",
                },
            )
            if sim is None:
                continue
            add(
                Candidate(
                    key=f"debt_catch_up:{g['id']}",
                    kind="debt_catch_up",
                    trigger_kind="goal_gap",
                    entities={"goal": g["name"]},
                    finding_ids=[str(f.id)],
                    evidence_txn_ids=[],
                    est_monthly_impact=Decimal(0),
                    est_total_impact=q(extra * 3),
                    facts={
                        "goal": g["name"],
                        "extra_payment": extra,
                        "remaining": g.get("remaining"),
                        "monthly_payment": g.get("monthly_payment"),
                        "required_monthly": g.get("required_monthly"),
                        "target_date": g.get("target_date"),
                        "simulation": sim,
                    },
                    scenario={
                        "type": "recurring_cost",
                        "amount": str(extra),
                        "frequency": "monthly",
                    },
                    simulation=sim,
                ),
                f.severity,
            )
            continue
        if g["kind"] == "savings_target":
            gap = Decimal(str(g.get("required_monthly") or 0)) - Decimal(
                str(g.get("monthly_savings") or 0)
            )
            if g["status"] == "not_on_track" or gap <= 0:
                gap = Decimal(str(g.get("remaining"))) / 12
            gap = q(gap)
            sim = _simulate(
                a, {"type": "savings_transfer", "amount": str(gap), "frequency": "monthly"}
            )
            if sim is None:
                continue
            add(
                Candidate(
                    key=f"goal_catch_up:{g['id']}",
                    kind="goal_catch_up",
                    trigger_kind="goal_gap",
                    entities={"goal": g["name"]},
                    finding_ids=[str(f.id)],
                    evidence_txn_ids=[],
                    est_monthly_impact=gap,
                    est_total_impact=q(gap * 3),
                    facts={
                        "goal": g["name"],
                        "monthly_transfer": gap,
                        "remaining": g.get("remaining"),
                        "eta": g.get("eta"),
                        "required_monthly": g.get("required_monthly"),
                        "projected_monthly_savings": g.get("monthly_savings"),
                        "simulation": sim,
                    },
                    scenario={
                        "type": "savings_transfer",
                        "amount": str(gap),
                        "frequency": "monthly",
                    },
                    simulation=sim,
                ),
                f.severity,
            )
        else:
            cat_key = _cap_stream(a, g)
            proj, cap = Decimal(str(g["projected"])), Decimal(str(g["cap"]))
            if cat_key is None or proj <= 0:
                continue
            pct = min(round(float((proj - cap) / proj) * 100), 60)
            sim = (
                _simulate(a, {"type": "category_cut", "category": cat_key, "pct": pct})
                if pct > 0
                else None
            )
            add(
                Candidate(
                    key=f"cap_overrun:{g['id']}",
                    kind="cap_overrun",
                    trigger_kind="goal_gap",
                    entities={"category": g.get("category") or g["name"], "goal": g["name"]},
                    finding_ids=[str(f.id)],
                    evidence_txn_ids=[],
                    est_monthly_impact=q(proj - cap),
                    est_total_impact=q(proj - cap),
                    facts={
                        "goal": g["name"],
                        "cap": cap,
                        "projected": proj,
                        "over_by": g.get("over_by"),
                        "cut_pct": pct,
                        "simulation": sim or {},
                    },
                    scenario={"type": "category_cut", "category": cat_key, "pct": pct}
                    if sim
                    else None,
                    simulation=sim or {},
                ),
                f.severity,
            )

    # 5. delay a risky planned purchase (latest what-if)
    for f in by_type.get("planned_purchase_risk", []):
        d = f.data
        lever = next((lv for lv in d.get("levers", []) if lv["kind"] == "delay"), None)
        if lever is None:
            continue
        sc = lever["scenario"]
        sim = _simulate(a, sc)
        if sim is None:
            continue
        add(
            Candidate(
                key=f"delay_purchase:{d['label']}",
                kind="delay_purchase",
                trigger_kind="forecast_shortfall",
                entities={"purchase": d["label"], "date": lever["title_data"]["date"]},
                finding_ids=[str(f.id)],
                evidence_txn_ids=[],
                est_monthly_impact=Decimal(0),
                est_total_impact=q(Decimal(lever["outcome"]["min_p10"]) - Decimal(d["min_p10"])),
                facts={
                    "purchase": d["label"],
                    "amount": d["amount"],
                    "planned_date": d["date"],
                    "delay_days": lever["title_data"]["days"],
                    "new_date": lever["title_data"]["date"],
                    "bad_case_low_now": d["min_p10"],
                    "bad_case_low_if_delayed": Decimal(lever["outcome"]["min_p10"]),
                    "simulation": sim,
                },
                scenario=sc,
                simulation=sim,
            ),
            f.severity,
        )

    # 6. fees: avoidable money
    for f in by_type.get("fees_paid", []):
        key = a.ctx.streams_by_category.get("fees/interest")
        sim = _simulate(a, {"type": "category_cut", "category": key, "pct": 100}) if key else None
        add(
            Candidate(
                key="fee_paydown",
                kind="fee_paydown",
                trigger_kind="pattern",
                entities={
                    "merchant": str(f.data.get("merchant") or ""),
                    "category": "Fees/Interest",
                },
                finding_ids=[str(f.id)],
                evidence_txn_ids=f.evidence_txn_ids[:5],
                est_monthly_impact=Decimal(f.data["monthly"]),
                est_total_impact=(sim or {}).get("end_change", Decimal(0)),
                facts={
                    "fees_last_90_days": f.data["total"],
                    "monthly": f.data["monthly"],
                    "count": f.data["count"],
                    "merchant": f.data.get("merchant"),
                    "simulation": sim or {},
                },
                scenario={"type": "category_cut", "category": key, "pct": 100} if sim else None,
                simulation=sim or {},
            ),
            f.severity,
        )

    # 7. ask for a refund of a duplicate charge (one-time; nothing to simulate)
    for f in by_type.get("duplicate_charge", []):
        d = f.data
        add(
            Candidate(
                key=f"refund_duplicate:{f.fingerprint}",
                kind="refund_duplicate",
                trigger_kind="anomaly",
                entities={"merchant": d["merchant"], "dates": d["dates"]},
                finding_ids=[str(f.id)],
                evidence_txn_ids=f.evidence_txn_ids,
                est_monthly_impact=Decimal(0),
                est_total_impact=d["extra_reporting"],
                facts={
                    "merchant": d["merchant"],
                    "charge": d["amount"],
                    "charge_currency": d["currency"],
                    "times": d["count"],
                    "dates": d["dates"],
                    "refund": d["extra_reporting"],
                    "simulation": "one-time refund, not simulated",
                },
            ),
            f.severity,
        )

    # 8. declare a confident detected repeat (makes the forecast firmer; no money moves)
    for f in by_type.get("recurring_not_declared", [])[:2]:
        d = f.data
        add(
            Candidate(
                key=f"promote_recurring:{d['merchant']}",
                kind="promote_recurring",
                trigger_kind="pattern",
                entities={"merchant": d["merchant"]},
                finding_ids=[str(f.id)],
                evidence_txn_ids=f.evidence_txn_ids,
                est_monthly_impact=Decimal(0),
                est_total_impact=Decimal(0),
                facts={
                    "merchant": d["merchant"],
                    "amount": d["amount"],
                    "amount_currency": d["currency"],
                    "every": d["period_noun"],
                    "charges_seen": d["count"],
                    "next_expected": d["next_expected"],
                },
            ),
            "info",
        )
    return sorted(out, key=lambda c: (-c.score, c.key))


def _rate_facts(a: AdviceInputs, currency: str) -> dict[str, Any]:
    """Amendment: a converted amount comes with its rate and rate date as facts."""
    if currency == a.currency:
        return {}
    r = a.ctx.rate(a.s, currency)
    return {
        "fx_rate": f"{r.rate.quantize(Decimal('0.0001'))} {a.currency} per {currency}",
        "fx_rate_value": r.rate,
        "fx_rate_date": r.date.isoformat() if r.date else None,
    }


def _top_discretionary(a: AdviceInputs) -> tuple[str, str, float] | None:
    st = a.inp.streams
    best = None
    for c, (key, label, nature) in enumerate(zip(st.keys, st.labels, st.natures, strict=True)):
        if nature != "discretionary":
            continue
        monthly = -float(a.base.stream_mean[:, c].sum()) / a.inp.horizon * 30.44
        if monthly > 0 and (best is None or monthly > best[2]):
            best = (key, label, monthly)
    return best


def _cap_stream(a: AdviceInputs, g: dict[str, Any]) -> str | None:
    name = (g.get("category") or "").lower()
    return a.ctx.streams_by_category.get(name)
