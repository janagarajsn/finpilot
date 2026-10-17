"""Transactions API (manual entry, edits, category corrections, FX overrides, export)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.api.routes_fx import fx_service
from finpilot.fx.service import FXService
from finpilot.recurring import service as rec
from finpilot.tenancy import Principal, get_db, get_principal
from finpilot.transactions import service as txns

router = APIRouter(prefix="/transactions", tags=["transactions"])


def create_txn(db: Session, uid: uuid.UUID, body: sc.TxnIn, fx: FXService) -> dict:
    inp = txns.TxnInput(
        kind=body.kind,
        amount=body.amount,
        date=body.date,
        description=body.description,
        currency=body.currency,
        category_id=body.category_id,
        account_id=body.account_id,
        notes=body.notes,
        fx_rate=body.fx_rate,
    )
    tid = txns.create(db, uid, inp, fx)
    assert tid is not None
    if body.repeat:
        rec.create_rule(
            db,
            uid,
            {
                "kind": body.kind,
                "description": body.description,
                "amount": body.amount,
                "currency": txns.get(db, uid, tid)["currency"],
                "category_id": body.category_id,
                "account_id": body.account_id,
                "frequency": body.repeat.frequency,
                "interval": body.repeat.interval,
                "start_date": body.date,
                "end_date": body.repeat.end_date,
                "notes": body.notes,
            },
            first_txn_id=tid,
        )
    return txns.get(db, uid, tid)


def _enqueue_categorization(user_id: uuid.UUID) -> None:
    from finpilot.tasks.ledger_tasks import categorize_pending

    try:
        categorize_pending.delay(str(user_id))
    except Exception:  # broker down: rows stay uncategorized until the next pass
        import logging

        logging.getLogger(__name__).warning("could not enqueue categorization", exc_info=True)


@router.post("", response_model=sc.TxnOut, status_code=status.HTTP_201_CREATED)
def create(
    body: sc.TxnIn,
    background: BackgroundTasks,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    out = create_txn(db, p.user_id, body, fx)
    if out["category_id"] is None:
        # Runs after the response, i.e. after the request transaction has committed.
        background.add_task(_enqueue_categorization, p.user_id)
    return out


@router.get("", response_model=sc.TxnListOut)
def list_(
    q: str | None = Query(default=None, max_length=100),
    category_id: uuid.UUID | None = None,
    uncategorized: bool = False,
    account_id: uuid.UUID | None = None,
    kind: Literal["expense", "income"] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    needs_review: bool | None = None,
    source: Literal["manual", "import", "recurring_rule"] | None = None,
    import_batch_id: uuid.UUID | None = None,
    sort: Literal["date_desc", "date_asc", "amount_desc", "amount_asc"] = "date_desc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    f = txns.TxnFilter(
        q=q,
        category_id=category_id,
        uncategorized=uncategorized,
        account_id=account_id,
        kind=kind,
        date_from=date_from,
        date_to=date_to,
        needs_review=needs_review,
        source=source,
        import_batch_id=import_batch_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    items, total = txns.list_(db, p.user_id, f)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/review-queue", response_model=sc.TxnListOut)
def review_queue(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    items, total = txns.list_(
        db, p.user_id, txns.TxnFilter(needs_review=True, limit=limit, offset=offset)
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/export.csv", response_class=Response)
def export(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> Response:
    body = txns.export_csv(db, p.user_id, txns.TxnFilter())
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="finpilot-transactions.csv"'},
    )


@router.get("/{txn_id}", response_model=sc.TxnOut)
def get(
    txn_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return txns.get(db, p.user_id, txn_id)


@router.patch("/{txn_id}", response_model=sc.TxnOut)
def patch(
    txn_id: uuid.UUID,
    body: sc.TxnPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    return txns.update(db, p.user_id, txn_id, body.model_dump(exclude_unset=True), fx)


@router.patch("/{txn_id}/fx", response_model=sc.TxnOut)
def patch_fx(
    txn_id: uuid.UUID,
    body: sc.TxnFxPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    return txns.override_fx(db, p.user_id, txn_id, body.fx_rate, fx)


@router.delete("/{txn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    txn_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> Response:
    txns.delete(db, p.user_id, txn_id)
    return Response(status_code=204)
