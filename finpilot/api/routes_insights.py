"""Analyzer output: insights (findings, patterns, trends, fx exposure) and the subscription audit."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.agents.analyzer import get_report, invalidate
from finpilot.api import schemas as sc
from finpilot.api.routes_forecast import llm_client
from finpilot.db.session import rowcount
from finpilot.insights.models import AnalysisReport
from finpilot.llm.client import LLMClient
from finpilot.recurring import detected
from finpilot.tenancy import Principal, get_db, get_principal
from finpilot.trail.narration import summarize_report

router = APIRouter(tags=["insights"])


@router.get("/insights", response_model=AnalysisReport)
def insights(
    refresh: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> AnalysisReport:
    """Served from the snapshot while data_version / currency / date are unchanged."""
    return get_report(db, p.user_id, refresh=refresh)


@router.post("/insights/findings/{finding_id}/{action}")
def finding_action(
    finding_id: uuid.UUID,
    action: str,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    if action not in ("dismiss", "restore"):
        raise HTTPException(404, "Unknown action.")
    n = rowcount(
        db.execute(
            text("""UPDATE analysis_findings SET dismissed_at = CASE WHEN :d THEN now() END
                    WHERE id = :id AND user_id = :u"""),
            {"d": action == "dismiss", "id": finding_id, "u": p.user_id},
        )
    )
    if n != 1:
        raise HTTPException(404, "Finding not found.")
    invalidate(db, p.user_id)
    return {"id": str(finding_id), "dismissed": action == "dismiss"}


@router.post("/insights/summary")
def insights_summary(
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
) -> dict:
    """2–4 sentence grounded summary of the top findings (cached per data_version)."""
    return summarize_report(db, p.user_id, get_report(db, p.user_id), llm)


@router.get("/subscriptions")
def subscriptions(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict:
    rep = get_report(db, p.user_id)
    return {
        "subscriptions": [s.model_dump(mode="json") for s in rep.subscriptions],
        "totals": rep.subscription_totals,
        "findings": [f.model_dump(mode="json") for f in rep.by_kind("subscription")],
    }


@router.post("/subscriptions/{det_id}/usage", response_model=sc.DetectedOut)
def subscription_usage(
    det_id: uuid.UUID,
    body: sc.UsageIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    """Usage rating 0–5 (0–1 = rarely used). The only way "unused" is ever asserted: by the user."""
    return detected.set_usage(db, p.user_id, det_id, body.rating)
