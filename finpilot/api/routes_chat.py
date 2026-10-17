"""Chat: a tool-using assistant over the user's own data. Answers are verified before they are
sent; `/chat/stream` streams tool progress (SSE) and then the verified message."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.agents import chat_agent
from finpilot.api.deps import limit
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import get_settings
from finpilot.llm.client import LLMClient
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/chat", tags=["chat"])
STREAM_TIMEOUT_S = 180


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=2, max_length=2000)
    session_id: uuid.UUID | None = None


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["up", "down"] | None


def _limit(request: Request, p: Principal) -> None:
    limit(request, "chat", get_settings().rl_chat_per_min, 60, key=str(p.user_id))


@router.post("")
def chat(
    body: ChatIn,
    request: Request,
    p: Principal = Depends(get_principal),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> dict[str, Any]:
    """Ask a question; returns the verified answer (and the session id to continue)."""
    _limit(request, p)
    try:
        r = chat_agent.respond(
            p.user_id,
            body.message,
            session_id=str(body.session_id) if body.session_id else None,
            llm=llm,
            fx=fx,
        )
    except chat_agent.ChatError as e:
        raise HTTPException(e.status, str(e)) from e
    return r.as_dict()


@router.post("/stream")
def chat_stream(
    body: ChatIn,
    request: Request,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> StreamingResponse:
    """Server-Sent Events: `session`, `progress` (one per tool step), then `message` (verified)
    or `error`."""
    _limit(request, p)
    if body.session_id:  # ownership before the stream starts: a foreign session is a plain 404
        try:
            chat_agent.get_session(db, p.user_id, body.session_id)
        except chat_agent.ChatError as e:
            raise HTTPException(e.status, str(e)) from e
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def work() -> None:
        try:
            chat_agent.respond(
                p.user_id,
                body.message,
                session_id=str(body.session_id) if body.session_id else None,
                llm=llm,
                fx=fx,
                emit=q.put,
            )
        except chat_agent.ChatError as e:
            q.put({"type": "error", "status": e.status, "detail": str(e)})
        except Exception:
            q.put({"type": "error", "status": 500, "detail": "Something went wrong."})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream() -> Iterator[str]:
        while True:
            try:
                ev = q.get(timeout=STREAM_TIMEOUT_S)
            except queue.Empty:
                yield 'event: error\ndata: {"detail": "timed out"}\n\n'
                return
            if ev is None:
                return
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def sessions(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    return chat_agent.list_sessions(db, p.user_id)


@router.get("/sessions/{session_id}")
def session(
    session_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    try:
        return chat_agent.get_session(db, p.user_id, session_id)
    except chat_agent.ChatError as e:
        raise HTTPException(e.status, str(e)) from e


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> None:
    try:
        chat_agent.delete_session(db, p.user_id, session_id)
    except chat_agent.ChatError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("/messages/{message_id}/feedback")
def feedback(
    message_id: uuid.UUID,
    body: FeedbackIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    try:
        return chat_agent.feedback(db, p.user_id, message_id, body.value)
    except chat_agent.ChatError as e:
        raise HTTPException(e.status, str(e)) from e
