"""Instant answers to common questions (intent router + layered cache)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.agents import intent_router
from finpilot.answers import service
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.llm.client import LLMClient
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/answers", tags=["answers"])
Period = Literal["this_month", "last_month", "30d", "3m", "6m", "12m"]


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=500)
    period: Period | None = None
    refresh: bool = False


@router.get("", response_model=service.AnswerOut)
def by_intent(
    intent: str,
    period: Period | None = None,
    refresh: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> service.AnswerOut:
    """Quick-question chips: answer a known intent (served from L2 when warm)."""
    if intent not in intent_router.answerable():
        raise HTTPException(404, "Unknown intent.")
    return service.answer(
        db, p.user_id, intent=intent, period=period, llm=llm, fx=fx, refresh=refresh, source="chip"
    )


@router.post("", response_model=service.AnswerOut)
def ask(
    body: AskIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> service.AnswerOut:
    """A typed question: routed to an intent (rules → embeddings → fast LLM), then answered."""
    from finpilot.agents.chat_agent import names_entity

    out = service.answer(
        db,
        p.user_id,
        question=body.question,
        period=body.period,
        llm=llm,
        fx=fx,
        refresh=body.refresh,
        source="ask",
    )
    out.suggest_chat = out.intent != "afford_purchase" and names_entity(
        db, p.user_id, body.question
    )
    return out


@router.get("/stats")
def stats(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    """Your answers by cache layer: count and p50/p95 latency (last 30 days)."""
    return service.layer_stats(db, p.user_id)
