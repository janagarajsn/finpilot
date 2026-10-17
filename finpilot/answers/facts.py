"""Deterministic facts per intent (numbers from code; the LLM only phrases them).

Every builder returns a JSON-safe dict whose amounts are Decimals in the reporting currency,
so the same dict feeds the template, the LLM prompt and the grounding guard.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.agents.advisor import list_recommendations
from finpilot.agents.analyzer import get_report
from finpilot.forecasting.service import get_forecast
from finpilot.insights import dashboard as dash


def _q(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def _share(part: Any, total: Any) -> float:
    t = float(total or 0)
    return round(float(part) / t * 100, 1) if t else 0.0


def build(s: Session, uid: uuid.UUID, intent: str, period: str, fx: Any = None) -> dict[str, Any]:
    d = dash.build(s, uid, period, fx)
    cur = d["reporting_currency"]
    k = d["kpis"]
    when = {
        "this_month": "this month",
        "last_month": "last month",
        "30d": "in the last 30 days",
        "3m": "in the last 3 months",
        "6m": "in the last 6 months",
        "12m": "in the last 12 months",
    }.get(period, "")
    base: dict[str, Any] = {
        "currency": cur,
        "period": period,
        "period_label": d["period_label"],
        "when": when,
        "from": d["window"][0].isoformat(),
        "to": d["window"][1].isoformat(),
        "has_data": d["counts"]["transactions"] > 0,
    }
    breakdown = [
        {
            "category": b["name"],
            "spend": _q(b["spend"]),
            "share_pct": _share(b["spend"], k["spend"]),
        }
        for b in d["breakdown"][:6]
    ]
    if intent == "spending_trend":
        rep = get_report(s, uid)
        movers = sorted(
            (t for t in rep.category_trends if t.mom_pct is not None and t.last_full_month > 0),
            key=lambda t: -(t.mom_pct or 0),
        )[:3]
        return base | {
            "spend": k["spend"],
            "previous_spend": k["spend_prev"],
            "spend_change_pct": k["spend_delta_pct"],
            "months": [
                {
                    "month": m["month"].isoformat()[:7]
                    if hasattr(m["month"], "isoformat")
                    else str(m["month"])[:7],
                    "spend": _q(m["spend"]),
                }
                for m in d["trend"][-6:]
            ],
            "rising_categories": [
                {"category": t.name, "last_month": t.last_full_month, "change_pct": t.mom_pct}
                for t in movers
                if (t.mom_pct or 0) > 0
            ],
        }
    if intent in ("where_overspending", "top_categories"):
        rep = get_report(s, uid)
        flags = [
            {"title": f.title, "detail": f.body}
            for f in rep.findings
            if f.type
            in ("category_spike", "category_trend_up", "concentration", "merchant_outlier")
        ][:3]
        return base | {"total_spend": k["spend"], "categories": breakdown, "flags": flags}
    if intent == "where_to_save":
        recs = list_recommendations(s, uid)["recommendations"]
        rep = get_report(s, uid)
        st = rep.subscription_totals or {}
        return base | {
            "recommendations": [
                {
                    "title": r["title"],
                    "action": r["action"],
                    "monthly_saving": _q(r["est_monthly_impact"])
                    if r["est_monthly_impact"]
                    else None,
                    "one_time": _q(r["est_total_impact"])
                    if not r["est_monthly_impact"] and r["est_total_impact"]
                    else None,
                }
                for r in recs[:3]
            ],
            "subscriptions_monthly": st.get("monthly"),
            "subscriptions_count": st.get("count"),
            "top_category": breakdown[0] if breakdown else None,
        }
    if intent == "upcoming_bills":
        items = [
            {
                "date": u["date"].isoformat(),
                "description": u["description"],
                "kind": u["kind"],
                "amount": _q(u["amount"]),
                "amount_currency": u["currency"],
                "expected": bool(u.get("estimated")),
            }
            for u in d["upcoming"]
        ]
        out_total = sum(
            (i["amount"] for i in items if i["kind"] == "expense" and i["amount_currency"] == cur),
            Decimal(0),
        )
        return base | {
            "items": items[:10],
            "count": len(items),
            "total_out": _q(out_total),
            "days": 14,
        }
    if intent == "subscription_review":
        rep = get_report(s, uid)
        subs = [
            {
                "merchant": x.merchant,
                "monthly": x.monthly_cost,
                "flags": x.flags,
                "rating": x.usage_rating,
            }
            for x in rep.subscriptions
            if x.status == "active"
        ]
        st = rep.subscription_totals or {}
        return base | {
            "subscriptions": subs[:10],
            "count": st.get("count", 0),
            "monthly": st.get("monthly", Decimal(0)),
            "annual": st.get("annual", Decimal(0)),
            "flagged": st.get("flagged", 0),
        }
    if intent == "goal_status":
        fc = get_forecast(s, uid, fx=fx)
        return base | {
            "goals": [
                {
                    k2: g.get(k2)
                    for k2 in (
                        "name",
                        "kind",
                        "status",
                        "eta",
                        "remaining",
                        "target",
                        "saved",
                        "paid",
                        "monthly_savings",
                        "required_monthly",
                        "cap",
                        "projected",
                        "over_by",
                        "category",
                    )
                }
                for g in fc.goals
            ]
        }
    if intent == "savings_rate":
        rep = get_report(s, uid)
        months = (rep.patterns.get("savings_rate") or {}).get("months", [])
        return base | {
            "income": k["income"],
            "spend": k["spend"],
            "net": k["net"],
            "savings_rate": k["savings_rate"],
            "previous_savings_rate": k["savings_rate_prev"],
            "recent_months": months[-3:],
        }
    if intent == "income_summary":
        start, end = d["window"]
        rows = s.execute(
            text("""SELECT coalesce(merchant_norm, raw_description) AS source, sum(amount_reporting) AS total, count(*) AS n
                    FROM v_transactions_reporting WHERE user_id = :u AND date BETWEEN :a AND :b AND amount_reporting > 0
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 5"""),
            {"u": uid, "a": start, "b": end},
        ).all()
        return base | {
            "income": k["income"],
            "previous_income": k["income_prev"],
            "income_change_pct": k["income_delta_pct"],
            "sources": [{"source": r[0], "amount": _q(r[1]), "count": r[2]} for r in rows],
        }
    # free_form: a general fact pack
    rep = get_report(s, uid)
    fc = get_forecast(s, uid, fx=fx)
    recs = list_recommendations(s, uid)["recommendations"]
    return base | {
        "income": k["income"],
        "spend": k["spend"],
        "savings_rate": k["savings_rate"],
        "balance": k["balance"],
        "top_categories": breakdown[:3],
        "top_findings": [{"title": f.title} for f in rep.top(None, 3)],
        "forecast": {
            "end_typical": fc.outcome.end_p50,
            "lowest_bad_case": fc.outcome.min_p10,
            "lowest_date": fc.outcome.min_p10_date.isoformat(),
            "verdict": fc.outcome.verdict,
        },
        "top_recommendation": recs[0]["title"] if recs else None,
    }
