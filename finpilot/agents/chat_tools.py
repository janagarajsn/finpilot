"""Chat agent tools: deterministic implementations of config/chat_tools.yml.

Each tool takes the model's (untrusted, schema-checked) arguments and returns a JSON-safe dict
computed by code. Amounts are Decimals; every dict that holds money carries a `currency` key, so
the same result feeds the grounding FactSet (`FactSet.add_obj`) and the model (inside <data>).
A tool never raises to the agent: problems come back as {"error": ...} so the model can recover.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.config import get_settings
from finpilot.insights import dashboard as dash
from finpilot.llm.client import LLMClient

log = logging.getLogger(__name__)
Q = Decimal("0.01")
FLAG_WORDS = {
    "price_hike": "price went up",
    "overlap": "overlaps with a similar service",
    "low_value_proxy": "likely low value (heuristic)",
}


@lru_cache
def config() -> dict[str, Any]:
    return dict(
        yaml.safe_load((get_settings().config_dir / "chat_tools.yml").read_text(encoding="utf-8"))
    )


def openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for name, spec in config()["tools"].items()
    ]


@dataclass
class ToolContext:
    s: Session
    uid: uuid.UUID
    llm: LLMClient | None
    fx: Any
    today: dt.date
    currency: str


def _q(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Q)


def _pct(cur: Decimal, prev: Decimal) -> float | None:
    return round(float((cur - prev) / prev * 100), 1) if prev else None


def _date(v: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _range(args: dict[str, Any], today: dt.date) -> tuple[dt.date, dt.date, dt.date, dt.date, str]:
    period = args.get("period") or "this_month"
    if period == "custom":
        start = _date(args.get("date_from")) or today.replace(day=1)
        end = min(_date(args.get("date_to")) or today, today)
        if start > end:
            start, end = end, start
        span = (end - start).days + 1
        return (
            start,
            end,
            start - dt.timedelta(days=span),
            start - dt.timedelta(days=1),
            f"{start.isoformat()} to {end.isoformat()}",
        )
    w = dash.window(period, today)
    return w.start, w.end, w.prev_start, w.prev_end, w.label


def _categories(c: ToolContext) -> list[str]:
    return [
        r[0]
        for r in c.s.execute(
            text("""SELECT name FROM categories WHERE (user_id = :u OR user_id IS NULL) AND NOT archived
                    ORDER BY name"""),
            {"u": c.uid},
        )
    ]


def _match_category(c: ToolContext, name: str) -> list[uuid.UUID]:
    return [
        r[0]
        for r in c.s.execute(
            text("""SELECT id FROM categories WHERE (user_id = :u OR user_id IS NULL)
                      AND (lower(name) = lower(:n) OR name ILIKE '%' || :n || '%' OR slug ILIKE '%' || :n || '%')"""),
            {"u": c.uid, "n": name.strip()[:60]},
        )
    ]


# ---------------------------------------------------------------------------- tools
def query_transactions(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    start, end, pstart, pend, label = _range(args, c.today)
    kind = args.get("kind") if args.get("kind") in ("expense", "income", "all") else "expense"
    where = ["v.user_id = :u", "coalesce(cat.flow_type, 'expense') <> 'transfer'"]
    params: dict[str, Any] = {"u": c.uid}
    if kind == "expense":
        where.append("v.amount_reporting < 0 AND coalesce(cat.flow_type, 'expense') <> 'savings'")
    elif kind == "income":
        where.append("v.amount_reporting > 0 AND coalesce(cat.flow_type, 'income') <> 'savings'")
    searched_descriptions = False
    if args.get("category"):
        ids = _match_category(c, str(args["category"]))
        if not ids and not args.get("merchant"):
            # "movie tickets", "cricket gear": not a category, but often words in descriptions
            args = {**args, "merchant": args["category"]}
            searched_descriptions = True
        elif not ids:
            return {
                "error": f"no category matches '{str(args['category'])[:60]}'",
                "categories": _categories(c),
            }
        else:
            where.append("v.category_id = ANY(:cats)")
            params["cats"] = ids
    if args.get("merchant"):
        where.append(
            "(coalesce(v.merchant_norm, '') ILIKE '%' || :m || '%' OR v.raw_description ILIKE '%' || :m || '%')"
        )
        params["m"] = str(args["merchant"]).strip()[:80]
    if args.get("min_amount") is not None:
        try:
            params["min"] = Decimal(str(args["min_amount"]))
            where.append("abs(v.amount_reporting) >= :min")
        except InvalidOperation:
            pass
    clause = " AND ".join(where)
    sign = "-" if kind == "expense" else ""
    base = f"""FROM v_transactions_reporting v LEFT JOIN categories cat ON cat.id = v.category_id
               WHERE {clause} AND v.date BETWEEN :a AND :b"""

    def total(a: dt.date, b: dt.date) -> tuple[Decimal, int]:
        row = c.s.execute(
            text(f"SELECT {sign}coalesce(sum(v.amount_reporting), 0), count(*) {base}"),
            {**params, "a": a, "b": b},
        ).one()
        return _q(row[0]), int(row[1])

    amount, n = total(start, end)
    out: dict[str, Any] = {
        "currency": c.currency,
        "kind": kind,
        "period": label,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total": amount,
        "count": n,
        "period_days": (end - start).days + 1,
        "period_months": round(((end - start).days + 1) / 30.44),
        "filters": {
            k: args[k]
            for k in ("category", "merchant", "min_amount")
            if args.get(k) and not (k == "category" and searched_descriptions)
        },
        "no_such_category_searched_descriptions": searched_descriptions or None,
    }
    if args.get("compare_previous"):
        prev, pn = total(pstart, pend)
        out["previous"] = {
            "from": pstart.isoformat(),
            "to": pend.isoformat(),
            "total": prev,
            "count": pn,
            "change": _q(amount - prev),
            "change_pct": _pct(amount, prev),
        }
    group = args.get("group_by") or "none"
    key = {
        "category": "coalesce(cat.name, 'Uncategorized')",
        "merchant": "coalesce(v.merchant_norm, v.raw_description)",
        "month": "to_char(date_trunc('month', v.date), 'YYYY-MM')",
        "week": "to_char(date_trunc('week', v.date), 'YYYY-MM-DD')",
    }.get(group)
    if key:
        order = "1" if group in ("month", "week") else "2 DESC"
        out["groups"] = [
            {
                "key": r[0],
                "total": _q(r[1]),
                "count": int(r[2]),
                "share_pct": round(float(_q(r[1]) / amount * 100), 1) if amount else None,
            }
            for r in c.s.execute(
                text(
                    f"SELECT {key}, {sign}sum(v.amount_reporting), count(*) {base} GROUP BY 1 ORDER BY {order} LIMIT 12"
                ),
                {**params, "a": start, "b": end},
            )
        ]
    largest = max(0, min(int(args.get("largest", 3) or 0), 10))
    if largest:
        out["largest"] = [
            {
                "date": r.date.isoformat(),
                "description": r.description,
                "category": r.category,
                "amount": _q(abs(r.amount_reporting)),
            }
            for r in c.s.execute(
                text(f"""SELECT v.date, coalesce(v.merchant_norm, v.raw_description) AS description,
                               coalesce(cat.name, 'Uncategorized') AS category, v.amount_reporting
                        {base} ORDER BY abs(v.amount_reporting) DESC LIMIT :n"""),
                {**params, "a": start, "b": end, "n": largest},
            )
        ]
    return out


def get_category_trend(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.analyzer import get_report

    want = str(args.get("category") or "").strip().lower()
    rep = get_report(c.s, c.uid)
    trends = rep.category_trends
    t = next((x for x in trends if x.name.lower() == want), None) or next(
        (x for x in trends if want and want in x.name.lower()), None
    )
    if t is None:
        return {
            "error": f"no spending history for a category matching '{want[:60]}'",
            "categories_with_spend": [x.name for x in trends],
        }
    months = max(2, min(int(args.get("months") or 6), 12))
    return {
        "currency": rep.reporting_currency,
        "category": t.name,
        "months": [
            {"month": str(m["month"])[:7], "spend": _q(m["spend"])} for m in t.months[-months:]
        ],
        "months_shown": months,
        "last_full_month": t.last_full_month,
        "month_before": t.previous_month,
        "trailing_3_month_average": t.trailing3_avg,
        "change_vs_month_before_pct": t.mom_pct,
        "change_vs_3_month_average_pct": t.vs_trailing3_pct,
        "month_to_date": t.month_to_date,
    }


def get_forecast(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.forecasting.service import get_forecast as fc_get

    fc = fc_get(c.s, c.uid, fx=c.fx)
    o = fc.outcome
    return {
        "currency": fc.currency,
        "as_of": fc.as_of.isoformat(),
        "days": fc.horizon,
        "start_balance": fc.start_balance,
        "verdict": o.verdict,
        "end_balance_typical": o.end_p50,
        "end_balance_bad_case": o.end_p10,
        "end_balance_good_case": o.end_p90,
        "lowest_balance_bad_case": o.min_p10,
        "lowest_balance_bad_case_date": o.min_p10_date.isoformat(),
        "chance_below_zero_pct": round(o.p_below_zero * 100, 1),
        "cash_buffer": fc.buffer,
        "month_ends": [
            {"date": m.date, "typical": m.p50, "bad_case": m.p10, "partial_month": m.partial}
            for m in fc.month_ends
        ],
        "projected_monthly_savings": fc.projected_monthly_savings,
        "low_confidence": fc.low_confidence,
    }


def simulate(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.simulator_graph import missing_slots, to_scenario
    from finpilot.forecasting import scenarios
    from finpilot.forecasting.inputs import ForecastError
    from finpilot.forecasting.service import simulate_scenario
    from finpilot.llm.schemas import ParsedScenario

    try:
        p = ParsedScenario.model_validate(args)
    except ValidationError as e:
        return {"error": "invalid scenario", "details": [x["msg"] for x in e.errors()][:3]}
    missing = missing_slots(p)
    if missing:
        return {"error": "missing details; ask the user", "missing": missing}
    try:
        sc = scenarios.parse(to_scenario(p, c.today))
        r = simulate_scenario(c.s, c.uid, sc, c.fx, persist=False)
    except (ValidationError, ValueError, ForecastError) as e:
        return {"error": f"could not simulate: {str(e)[:200]}"}

    def side(o: Any) -> dict[str, Any]:
        return {
            "verdict": o.verdict,
            "end_balance_typical": o.end_p50,
            "lowest_balance_bad_case": o.min_p10,
            "lowest_balance_bad_case_date": o.min_p10_date.isoformat(),
            "chance_below_zero_pct": round(o.p_below_zero * 100, 1),
        }

    return {
        "currency": r.currency,
        "days": r.horizon,
        "cash_buffer": r.buffer,
        "scenario": r.scenario,
        "without_change": side(r.baseline),
        "with_change": side(r.result),
        "end_balance_change": r.end_delta_p50,
        "fx_used": r.fx_used,
        "ways_to_make_it_safer": [
            {
                "kind": lv.kind,
                "details": lv.title_data,
                "lowest_balance_bad_case": lv.outcome.min_p10,
                "improvement": lv.improvement,
            }
            for lv in r.levers[:2]
        ],
        "low_confidence": r.low_confidence,
    }


def list_subscriptions(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.analyzer import get_report

    rep = get_report(c.s, c.uid)
    st = rep.subscription_totals or {}
    return {
        "currency": rep.reporting_currency,
        "subscriptions": [
            {
                "merchant": x.merchant,
                "period": x.period,
                "monthly_cost": x.monthly_cost,
                "annual_cost": x.annual_cost,
                "charge": x.amount,
                "charge_currency": x.currency,
                "next_expected": x.next_expected.isoformat(),
                "flags": [FLAG_WORDS.get(f, f) for f in x.flags],
                "your_usage_rating_out_of_5": x.usage_rating,
            }
            for x in rep.subscriptions
            if x.status == "active"
        ],
        "count": st.get("count", 0),
        "monthly_total": st.get("monthly", Decimal(0)),
        "annual_total": st.get("annual", Decimal(0)),
    }


def get_goal_status(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.forecasting.service import get_forecast as fc_get

    fc = fc_get(c.s, c.uid, fx=c.fx)
    keys = (
        "name",
        "kind",
        "status",
        "on_track",
        "eta",
        "target",
        "saved",
        "paid",
        "remaining",
        "monthly_savings",
        "required_monthly",
        "deadline",
        "cap",
        "projected",
        "over_by",
        "category",
    )
    return {
        "currency": fc.currency,
        "goals": [{k: g.get(k) for k in keys if g.get(k) is not None} for g in fc.goals],
    }


def list_recurring(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.recurring import detected as det
    from finpilot.recurring import service as rec

    rules = [
        {
            "description": r["description"],
            "kind": r["kind"],
            "amount": _q(r["amount"]),
            "currency": r["currency"],
            "frequency": r["frequency"],
            "next_date": r["next_run_date"].isoformat() if r.get("next_run_date") else None,
            "status": r["status"],
        }
        for r in rec.list_rules(c.s, c.uid)
    ]
    found = [
        {
            "description": d.get("merchant_norm") or d.get("description"),
            "kind": d["kind"],
            "typical_amount": _q(d["amount_typical"]),
            "currency": d["currency"],
            "period": d["period"],
            "next_expected": d["next_expected"].isoformat() if d.get("next_expected") else None,
            "charges_seen": d["n_occurrences"],
        }
        for d in det.list_detected(c.s, c.uid)
    ]
    up = dash.build(c.s, c.uid, "30d", c.fx)["upcoming"]
    return {
        "currency": c.currency,
        "scheduled_rules": rules[:20],
        "detected_repeats": found[:20],
        "next_14_days": [
            {
                "date": u["date"].isoformat(),
                "description": u["description"],
                "kind": u["kind"],
                "amount": _q(u["amount"]),
                "currency": u["currency"],
                "estimated": bool(u.get("estimated")),
            }
            for u in up[:15]
        ],
    }


def explain_recommendation(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.agents.advisor import AdviceError, get_trail, list_recommendations

    rid = str(args.get("id") or "").strip()
    if not rid:
        recs = list_recommendations(c.s, c.uid)["recommendations"]
        return {
            "currency": c.currency,
            "recommendations": [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "action": r["action"],
                    "status": r["status"],
                    "estimated_monthly_impact": r["est_monthly_impact"],
                    "estimated_total_impact": r["est_total_impact"],
                }
                for r in recs
            ],
        }
    try:
        t = get_trail(c.s, c.uid, uuid.UUID(rid))
    except (ValueError, AdviceError):
        return {"error": "no recommendation with that id; call without an id to list them"}
    return {
        "currency": c.currency,
        "title": t["title"],
        "action": t["action"],
        "why": t.get("why"),
        "trigger": t.get("trail_trigger"),
        "reasoning": t.get("reasoning_text"),
        "estimated_monthly_impact": t.get("est_monthly_impact"),
        "estimated_total_impact": t.get("est_total_impact"),
        "findings": [{"title": f["title"], "detail": f["body"]} for f in t["findings"]],
        "forecast": t.get("forecast_ref_json"),
    }


def recall_memory(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.memory.retrieval import recall

    q = str(args.get("query") or "").strip()[:300]
    if not q:
        return {"error": "query is required"}
    return {
        "memories": [
            {"kind": m["kind"], "text": m["text"], "since": m["since"]}
            for m in recall(c.s, c.uid, q, c.llm)
        ]
    }


def convert_currency(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from finpilot.fx.service import FXUnavailable
    from finpilot.money import get_currency_registry

    if c.fx is None:
        return {"error": "exchange rates are unavailable right now"}
    try:
        amount = Decimal(str(args.get("amount")))
        reg = get_currency_registry()
        f, t = reg.get(str(args.get("from"))).code, reg.get(str(args.get("to"))).code
    except (InvalidOperation, KeyError, ValueError):
        return {"error": "unsupported amount or currency", "supported": ["INR", "USD"]}
    try:
        conv = c.fx.convert(amount, f, t, _date(args.get("date")))
    except FXUnavailable as e:
        return {"error": str(e)[:200]}
    return {
        "currency": f,
        "amount": amount,
        "converted": {"currency": t, "amount": conv.converted},
        "rate": {"currency": t, "per_1": conv.rate, "description": f"{t} per 1 {f}"},
        "rate_date": conv.rate_date.isoformat(),
        "source": conv.source,
        "approximate": conv.approximate,
        "stale": conv.stale,
    }


TOOLS: dict[str, Callable[[ToolContext, dict[str, Any]], dict[str, Any]]] = {
    "query_transactions": query_transactions,
    "get_category_trend": get_category_trend,
    "get_forecast": get_forecast,
    "simulate": simulate,
    "list_subscriptions": list_subscriptions,
    "get_goal_status": get_goal_status,
    "list_recurring": list_recurring,
    "explain_recommendation": explain_recommendation,
    "recall_memory": recall_memory,
    "convert_currency": convert_currency,
    "decline_off_topic": lambda c, a: {"declined": True},  # handled by the chat agent
}


def run_tool(c: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = TOOLS.get(name)
    if fn is None or name not in config()["tools"]:
        return {"error": f"unknown tool {name}"}
    try:
        with c.s.begin_nested():  # a failing tool must not poison the request transaction
            return fn(c, args if isinstance(args, dict) else {})
    except Exception as e:  # tools never crash the conversation
        log.warning("chat tool %s failed", name, exc_info=True)
        return {"error": f"{name} failed: {type(e).__name__}"}
