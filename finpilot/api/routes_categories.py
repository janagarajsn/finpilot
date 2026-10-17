"""Categories API: system taxonomy (read-only, hideable) + the user's own categories."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.categories import service as cats
from finpilot.ingest.normalizer import normalize_merchant
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[sc.CategoryOut])
def list_(
    include_archived: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict]:
    return cats.list_categories(db, p.user_id, include_archived=include_archived)


@router.get("/suggest", response_model=list[sc.SuggestionOut])
def suggest(
    q: str = Query(default="", max_length=200),
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict]:
    return cats.suggest(db, p.user_id, q, normalize_merchant(q) if q else None)


@router.post("", response_model=sc.CategoryOut, status_code=status.HTTP_201_CREATED)
def create(
    body: sc.CategoryIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return {
        **cats.create_category(db, p.user_id, body.model_dump()),
        "hidden": False,
        "txn_count": 0,
    }


@router.patch("/{category_id}", response_model=sc.CategoryOut)
def patch(
    category_id: uuid.UUID,
    body: sc.CategoryPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    changes = body.model_dump(exclude_unset=True)
    hidden = changes.pop("hidden", None)
    if hidden is not None:
        cats.set_hidden(db, p.user_id, category_id, hidden)
    if changes:
        cats.update_category(db, p.user_id, category_id, changes)
    return next(
        c
        for c in cats.list_categories(db, p.user_id, include_archived=True)
        if c["id"] == category_id
    )


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    category_id: uuid.UUID,
    reassign_to: uuid.UUID | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> Response:
    cats.delete_category(db, p.user_id, category_id, reassign_to)
    return Response(status_code=204)


@router.post("/{category_id}/merge", response_model=sc.Message)
def merge(
    category_id: uuid.UUID,
    body: sc.MergeIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    moved = cats.merge_categories(db, p.user_id, category_id, body.into_id)
    return {"detail": f"Merged. {moved} transactions moved."}


@router.post("/recategorize/preview", response_model=list[sc.RecatCandidateOut])
def recat_preview(
    body: sc.RecatIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list:
    return [c.__dict__ for c in cats.recategorize_preview(db, p.user_id, body.category_id)]


@router.post("/recategorize/apply", response_model=sc.Message)
def recat_apply(
    body: sc.RecatIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    n = cats.recategorize_apply(db, p.user_id, body.category_id, body.transaction_ids)
    return {"detail": f"{n} transactions re-categorized."}
