"""LLM narration behind the grounding guard (what-if answers, Insights summary).

Both fall back to deterministic text when the guard rejects the model three times or Mesh is
unavailable. The facts given to the model are exactly the facts the guard checks against.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.forecasting.simulator import SimulationResult
from finpilot.insights.models import AnalysisReport
from finpilot.llm.client import LLMClient
from finpilot.llm.schemas import NarrationResponse
from finpilot.money import get_currency_registry
from finpilot.trail.grounding import FactSet, guarded_json

SUMMARY_KIND = "analysis_summary"


def _facts_of_simulation(r: SimulationResult) -> tuple[FactSet, dict[str, Any]]:
    data = {
        "currency": r.currency,
        "cushion": r.buffer,
        "horizon_days": r.horizon,
        "without_change": r.baseline.model_dump(),
        "with_change": r.result.model_dump(),
        "expected_balance_change": r.end_delta_mean,
        "applied": r.applied,
        "exchange_rates_used": r.fx_used,
        "verified_levers": [
            {
                "kind": lv.kind,
                **lv.title_data,
                "bad_case_low_after": lv.outcome.min_p10,
                "verdict_after": lv.outcome.verdict,
            }
            for lv in r.levers
        ],
    }
    fs = FactSet().add_obj(data, r.currency)
    for fx in r.fx_used:  # converted figures are facts only as computed by code
        fs.add_number(fx["rate"])
    fs.add_number(10).add_number(90)  # "1 in 10", "90 days"
    return fs, data


def narrate_simulation(
    llm: LLMClient | None,
    r: SimulationResult,
    question: str,
    fallback_text: str,
    user_id: uuid.UUID,
) -> tuple[str, dict[str, Any]]:
    if llm is None or not llm.available:
        return fallback_text, {"generated_by": "template", "reason": "Mesh unavailable"}
    facts, data = _facts_of_simulation(r)
    reg = get_currency_registry().get(r.currency)
    out, guard = guarded_json(
        llm,
        "simulator_parser/verbalize",
        {
            "currency": r.currency,
            "symbol": reg.symbol,
            "question": question[:500],
            "verdict": r.result.verdict,
            "facts": json.dumps(data, default=str, ensure_ascii=False, indent=1),
        },
        NarrationResponse,
        facts=facts,
        texts=lambda x: [x.answer],
        fallback=lambda: NarrationResponse(answer=fallback_text),
        agent="simulator_parser",
        user_id=user_id,
        cache_scope=str(user_id),
    )
    return out.answer, {
        "generated_by": "template" if guard.fallback_used else "llm",
        **guard.as_dict(),
    }


def _facts_of_report(rep: AnalysisReport) -> tuple[FactSet, list[dict[str, Any]], dict[str, Any]]:
    fs = FactSet()
    top = [f for f in rep.findings if f.severity != "info" and f.type != "recurring_not_declared"][
        :8
    ]
    items = []
    for f in top:
        fs.add_obj(
            {
                "currency": f.currency or rep.reporting_currency,
                "baseline": f.baseline,
                "observed": f.observed,
                "threshold": f.threshold,
            },
            rep.reporting_currency,
        )
        fs.add_obj(f.data, f.data.get("currency") or rep.reporting_currency)
        fs.add_date(f.period_start).add_date(f.period_end)
        items.append({"title": f.title, "detail": f.body, "severity": f.severity})
    subs = {
        "count": rep.subscription_totals.get("count"),
        "monthly": rep.subscription_totals.get("monthly"),
        "annual": rep.subscription_totals.get("annual"),
        "currency": rep.reporting_currency,
    }
    fs.add_obj(subs, rep.reporting_currency).add_date(rep.as_of)
    return fs, items, subs


def summary_fallback(rep: AnalysisReport) -> str:
    top = [f.title for f in rep.findings if f.severity != "info"][:3]
    return " · ".join(top) if top else "Nothing unusual in your recent transactions."


def summarize_report(
    s: Session, user_id: uuid.UUID, rep: AnalysisReport, llm: LLMClient | None
) -> dict[str, Any]:
    """Cached per (data_version, currency, as_of) in insight_snapshots(kind='analysis_summary')."""
    row = s.execute(
        text("""SELECT payload_json FROM insight_snapshots WHERE user_id = :u AND kind = :k
                AND data_version = :dv AND reporting_currency = :cur AND as_of = :d"""),
        {
            "u": user_id,
            "k": SUMMARY_KIND,
            "dv": rep.data_version,
            "cur": rep.reporting_currency,
            "d": rep.as_of,
        },
    ).first()
    if row:
        return dict(row[0]) | {"cached": True}
    if not [f for f in rep.findings if f.severity != "info"]:
        payload: dict[str, Any] = {
            "answer": summary_fallback(rep),
            "generated_by": "template",
            "grounding": {},
        }
    elif llm is None or not llm.available:
        payload = {
            "answer": summary_fallback(rep),
            "generated_by": "template",
            "grounding": {"reason": "Mesh unavailable"},
        }
    else:
        facts, items, subs = _facts_of_report(rep)
        reg = get_currency_registry().get(rep.reporting_currency)
        out, guard = guarded_json(
            llm,
            "analyzer_narrator/summary",
            {
                "as_of": rep.as_of.isoformat(),
                "currency": rep.reporting_currency,
                "symbol": reg.symbol,
                "findings": json.dumps(items, ensure_ascii=False, indent=1),
                "subscriptions": json.dumps(subs, default=str),
            },
            NarrationResponse,
            facts=facts,
            texts=lambda x: [x.answer],
            fallback=lambda: NarrationResponse(answer=summary_fallback(rep)),
            agent="analyzer_narrator",
            user_id=user_id,
            cache_scope=str(user_id),
        )
        payload = {
            "answer": out.answer,
            "generated_by": "template" if guard.fallback_used else "llm",
            "grounding": guard.as_dict(),
        }
    s.execute(
        text("""INSERT INTO insight_snapshots (user_id, kind, data_version, reporting_currency, as_of, payload_json)
                VALUES (:u, :k, :dv, :cur, :d, CAST(:p AS jsonb))
                ON CONFLICT (user_id, kind) DO UPDATE SET data_version = EXCLUDED.data_version,
                  reporting_currency = EXCLUDED.reporting_currency, as_of = EXCLUDED.as_of,
                  payload_json = EXCLUDED.payload_json, created_at = now()"""),
        {
            "u": user_id,
            "k": SUMMARY_KIND,
            "dv": rep.data_version,
            "cur": rep.reporting_currency,
            "d": rep.as_of,
            "p": json.dumps(payload, default=str),
        },
    )
    return payload | {"cached": False}
