"""What FinPilot remembers about you: list, edit, pin, delete, wipe (privacy)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.api.routes_forecast import llm_client
from finpilot.llm.client import LLMClient
from finpilot.memory.long_term import PgMemory
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=500)
    pinned: bool | None = None
    importance: float | None = Field(default=None, ge=0, le=1)


def _out(m: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in m.items()}


@router.get("")
def list_memories(
    include_inactive: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    """Active memories (pinned first); `include_inactive` adds superseded and expired ones."""
    return [_out(m) for m in PgMemory().list_all(db, p.user_id, include_inactive)]


@router.patch("/{memory_id}")
def update_memory(
    memory_id: uuid.UUID,
    body: MemoryPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    llm: LLMClient = Depends(llm_client),
) -> dict[str, Any]:
    try:
        m = PgMemory(llm).update(
            db,
            p.user_id,
            memory_id,
            text_=body.text,
            pinned=body.pinned,
            importance=body.importance,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if m is None:
        raise HTTPException(404, "Memory not found.")
    return _out(m)


@router.delete("/{memory_id}", status_code=204)
def delete_memory(
    memory_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> None:
    if not PgMemory().delete(db, p.user_id, memory_id):
        raise HTTPException(404, "Memory not found.")


@router.delete("")
def wipe(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict[str, int]:
    """Forget everything (memories and their vectors)."""
    return {"deleted": PgMemory().wipe(db, p.user_id)}
