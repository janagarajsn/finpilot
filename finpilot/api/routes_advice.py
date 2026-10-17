"""Recommendations (advisor) and their decision trails."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.agents import advisor
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.llm.client import LLMClient
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/recommendations", tags=["advice"])


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["up", "down"] | None


@router.get("")
def list_(
    all: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    """Latest advisor run; `stale` when financial data changed since it ran."""
    return advisor.list_recommendations(db, p.user_id, include_all=all)


@router.post("/refresh")
def refresh(
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: Any = Depends(fx_service),
    llm: LLMClient = Depends(llm_client),
) -> dict[str, Any]:
    run = advisor.Advisor(llm).run(db, p.user_id, fx=fx)
    return {
        "run_id": run.run_id,
        "generated_by": run.generated_by,
        "candidates": run.n_candidates,
        "duration_ms": run.duration_ms,
        **advisor.list_recommendations(db, p.user_id),
    }


@router.get("/{rec_id}/trail")
def trail(
    rec_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    return advisor.get_trail(db, p.user_id, rec_id)


@router.post("/{rec_id}/feedback")
def feedback(
    rec_id: uuid.UUID,
    body: FeedbackIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    return advisor.feedback(db, p.user_id, rec_id, body.value)


@router.post("/{rec_id}/{action}")
def decide(
    rec_id: uuid.UUID,
    action: Literal["accept", "dismiss"],
    body: DecideIn | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    """Dismissed advice (with its reason) is not proposed again for 90 days."""
    return advisor.decide(db, p.user_id, rec_id, action, body.reason if body else None)
