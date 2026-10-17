"""Coordinator runs: start, list, inspect, live progress (SSE), resume."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.agents import coordinator, intent_router
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.llm.client import LLMClient
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
SSE_MAX_SECONDS = 300


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "refresh_insights", "ask_affordability", "answer_from_cache", "free_chat", "explain"
    ] = "refresh_insights"
    question: str | None = Field(default=None, max_length=500)
    recommendation_id: uuid.UUID | None = None
    answer_intent: str | None = Field(default=None, max_length=40)  # answer_from_cache: a chip
    session_id: uuid.UUID | None = None  # free_chat: continue a conversation


def _start(user_id: str, run_id: str) -> None:
    from finpilot.tasks.pipeline_tasks import run_pipeline

    run_pipeline.delay(user_id, "manual", run_id)


def _resume(user_id: uuid.UUID, run_id: str) -> None:
    from finpilot.fx.service import build_fx_service
    from finpilot.llm.client import get_llm

    coordinator.resume_run(user_id, run_id, llm=get_llm(), fx=build_fx_service())


@router.post("/run", status_code=202)
def start(
    body: RunIn,
    background: BackgroundTasks,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> dict[str, Any]:
    """refresh_insights runs in the background (follow /runs/{id}/events); the other intents
    answer directly through the same graph (route_intent)."""
    if body.intent == "refresh_insights":
        run_id = coordinator.create_run(p.user_id, "manual", s=db)
        background.add_task(_start, str(p.user_id), run_id)  # after commit (scope=function)
        return {"run_id": run_id, "status": "running"}
    if body.intent in ("ask_affordability", "free_chat") and not body.question:
        raise coordinator.PipelineError("A question is required.", 422)
    if body.intent == "answer_from_cache" and not (body.question or body.answer_intent):
        raise coordinator.PipelineError("A question or an answer intent is required.", 422)
    if body.answer_intent and body.answer_intent not in intent_router.answerable():
        raise coordinator.PipelineError("Unknown answer intent.", 422)
    if body.intent == "explain" and not body.recommendation_id:
        raise coordinator.PipelineError("A recommendation id is required.", 422)
    run_id = coordinator.create_run(p.user_id, "manual")
    out = coordinator.run(
        p.user_id,
        run_id,
        intent=body.intent,
        llm=llm,
        fx=fx,
        payload={
            "question": body.question,
            "recommendation_id": str(body.recommendation_id) if body.recommendation_id else None,
            "answer_intent": body.answer_intent,
            "session_id": str(body.session_id) if body.session_id else None,
        },
    )
    if out["status"] == "failed":
        raise coordinator.PipelineError(
            out["error"] or "The run failed.", 404 if "not found" in (out["error"] or "") else 500
        )
    return {
        "run_id": run_id,
        "status": out["status"],
        "degraded": out["degraded"],
        "answer": out["answer"],
    }


@router.get("/runs")
def runs(
    limit: int = 20,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    return coordinator.list_runs(db, p.user_id, max(1, min(limit, 100)))


@router.get("/runs/{run_id}")
def run_detail(
    run_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    return coordinator.get_run(db, p.user_id, run_id)


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> StreamingResponse:
    """Server-Sent Events: the run's progress document each time it changes, until it ends."""
    first = coordinator.get_run(db, p.user_id, run_id)  # ownership check (404 for other users)

    def stream() -> Iterator[str]:
        last = None
        deadline = time.monotonic() + SSE_MAX_SECONDS
        doc: dict[str, Any] | None = first.get("progress") or {
            "status": first["status"],
            "steps": [],
        }
        while time.monotonic() < deadline:
            doc = coordinator.read_progress(str(run_id)) or doc
            payload = json.dumps(doc, default=str)
            if payload != last:
                last = payload
                yield f"event: progress\ndata: {payload}\n\n"
            if (doc or {}).get("status") != "running":
                yield f"event: done\ndata: {json.dumps({'status': (doc or {}).get('status')})}\n\n"
                return
            time.sleep(0.5)
        yield "event: timeout\ndata: {}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/resume", status_code=202)
def resume(
    run_id: uuid.UUID,
    background: BackgroundTasks,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    run = coordinator.get_run(db, p.user_id, run_id)
    if run["status"] == "running":
        raise coordinator.PipelineError("This run is still running.", 409)
    if run["status"] == "succeeded":
        raise coordinator.PipelineError("This run already finished.", 409)
    background.add_task(_resume, p.user_id, str(run_id))
    return {"run_id": str(run_id), "status": "resuming"}
