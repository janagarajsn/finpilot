"""User-declared recurring expenses/incomes."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.recurring import detected
from finpilot.recurring import service as rec
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/recurring", tags=["recurring"])


# Detected repeats are registered BEFORE "/{rule_id}" so "detected" is never parsed as a rule id.
@router.get("/detected", response_model=list[sc.DetectedOut])
def list_detected(
    all: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict]:
    return detected.list_detected(db, p.user_id, include_all=all)


@router.post(
    "/detected/{det_id}/promote", response_model=sc.PromoteOut, status_code=status.HTTP_201_CREATED
)
def promote_detected(
    det_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return detected.promote(db, p.user_id, det_id)


@router.post("/detected/{det_id}/{action}", response_model=sc.DetectedOut)
def detected_action(
    det_id: uuid.UUID,
    action: Literal["dismiss", "restore"],
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return detected.dismiss(db, p.user_id, det_id, dismissed=action == "dismiss")


@router.get("", response_model=list[sc.RecurringOut])
def list_(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> list[dict]:
    return rec.list_rules(db, p.user_id)


@router.post("", response_model=sc.RecurringOut, status_code=status.HTTP_201_CREATED)
def create(
    body: sc.RecurringIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return rec.create_rule(db, p.user_id, body.model_dump())


@router.get("/{rule_id}", response_model=sc.RecurringOut)
def get(
    rule_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return rec.with_preview(db, p.user_id, rec.get_rule(db, p.user_id, rule_id))


@router.patch("/{rule_id}", response_model=sc.RecurringOut)
def patch(
    rule_id: uuid.UUID,
    body: sc.RecurringPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return rec.update_rule(db, p.user_id, rule_id, body.model_dump(exclude_unset=True))


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    rule_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> Response:
    rec.delete_rule(db, p.user_id, rule_id)
    return Response(status_code=204)


@router.post("/{rule_id}/{action}", response_model=sc.RecurringOut)
def action(
    rule_id: uuid.UUID,
    action: Literal["pause", "resume", "skip-next", "end"],
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return rec.set_status(db, p.user_id, rule_id, action)
