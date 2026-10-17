"""Forecast, what-if simulator (structured + natural language with clarifying questions), goals."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.agents import simulator_graph
from finpilot.api.routes_fx import fx_service
from finpilot.forecasting import goals as goals_mod
from finpilot.forecasting import scenarios
from finpilot.forecasting.service import ForecastResult, get_forecast, simulate_scenario
from finpilot.llm.client import LLMClient, get_llm
from finpilot.recurring.service import user_today
from finpilot.tenancy import Principal, get_db, get_principal
from finpilot.trail.narration import narrate_simulation

router = APIRouter(tags=["forecast"])


def llm_client() -> LLMClient:
    return get_llm()


@router.get("/forecast", response_model=ForecastResult)
def forecast(
    refresh: bool = False,
    horizon: int | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: Any = Depends(fx_service),
) -> ForecastResult:
    """Baseline daily projection (p10/p50/p90, month ends, risk probabilities, goal projections)."""
    return get_forecast(db, p.user_id, fx=fx, refresh=refresh, horizon=horizon)


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: scenarios.Scenario
    horizon: int | None = Field(default=None, ge=7, le=366)


@router.post("/simulate")
def simulate(
    body: SimulateIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: Any = Depends(fx_service),
    llm: LLMClient = Depends(llm_client),
) -> dict[str, Any]:
    """The answer is written by the LLM under the grounding guard (template when it fails)."""
    res = simulate_scenario(db, p.user_id, body.scenario, fx=fx, horizon=body.horizon)
    answer, narration = narrate_simulation(
        llm,
        res,
        json.dumps(body.scenario.model_dump(mode="json")),
        simulator_graph.answer_text(res),
        p.user_id,
    )
    return {"result": res.model_dump(mode="json"), "answer": answer, "narration": narration}


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=300)


@router.post("/simulate/ask")
def ask(
    body: AskIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: Any = Depends(fx_service),
    llm: LLMClient = Depends(llm_client),
) -> dict[str, Any]:
    """'Can I afford a ₹80,000 trip next month?' → result, or a clarifying question + thread_id."""
    return simulator_graph.ask(db, p.user_id, body.question, llm, fx, user_today(db, p.user_id))


@router.post("/simulate/ask/{thread_id}/resume")
def ask_resume(
    thread_id: str,
    body: ResumeIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: Any = Depends(fx_service),
    llm: LLMClient = Depends(llm_client),
) -> dict[str, Any]:
    return simulator_graph.resume(
        db, p.user_id, thread_id[:80], body.answer, llm, fx, user_today(db, p.user_id)
    )


# ---------------------------------------------------------------------------- goals
class GoalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["savings_target", "spend_cap", "debt_payoff"]
    name: str = Field(min_length=1, max_length=100)
    target_amount: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    saved_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=16, decimal_places=2)
    target_date: dt.date | None = None
    category_id: uuid.UUID | None = None


class GoalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    target_amount: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=2)
    saved_amount: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    target_date: dt.date | None = None
    category_id: uuid.UUID | None = None
    status: Literal["active", "achieved", "archived"] | None = None


class GoalOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    target_amount: Decimal
    currency: str
    saved_amount: Decimal
    target_date: dt.date | None
    category_id: uuid.UUID | None
    category_name: str | None
    status: str


@router.get("/goals", response_model=list[GoalOut])
def list_goals(
    all: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict]:
    return goals_mod.list_goals(db, p.user_id, include_archived=all)


@router.post("/goals", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(
    body: GoalIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return goals_mod.create_goal(db, p.user_id, body.model_dump())


@router.get("/goals/{goal_id}", response_model=GoalOut)
def get_goal(
    goal_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return goals_mod.get_goal(db, p.user_id, goal_id)


@router.get("/goals/{goal_id}/snapshots")
def goal_snapshots(
    goal_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    """Weekly goal-tracker history (one point per ISO week)."""
    goals_mod.get_goal(db, p.user_id, goal_id)
    from finpilot.agents.goal_tracker import history

    return history(db, p.user_id, goal_id)


@router.patch("/goals/{goal_id}", response_model=GoalOut)
def patch_goal(
    goal_id: uuid.UUID,
    body: GoalPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return goals_mod.update_goal(db, p.user_id, goal_id, body.model_dump(exclude_unset=True))


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(
    goal_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> Response:
    goals_mod.delete_goal(db, p.user_id, goal_id)
    return Response(status_code=204)
