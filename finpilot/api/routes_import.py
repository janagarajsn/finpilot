"""Import wizard API: template downloads, upload → map → preview → confirm → undo."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.api.routes_fx import fx_service
from finpilot.categories import service as cats
from finpilot.db.repos import users as users_repo
from finpilot.db.session import user_session
from finpilot.fx.service import FXService
from finpilot.ingest import csv_parser as P
from finpilot.ingest import import_service as imp
from finpilot.ingest import template
from finpilot.money import get_currency_registry
from finpilot.tasks.ledger_tasks import run_import
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(prefix="/import", tags=["import"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/schema")
def schema(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict:
    profile = users_repo.get_profile(db, p.user_id)
    visible = [c for c in cats.list_categories(db, p.user_id) if not c["hidden"]]
    return template.schema(visible, get_currency_registry().codes, profile["reporting_currency"])


@router.get("/template.csv", response_class=Response)
def template_csv(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> Response:
    cur = users_repo.get_profile(db, p.user_id)["reporting_currency"]
    return Response(
        template.build_csv(cur),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="finpilot-template.csv"'},
    )


@router.get("/template.xlsx", response_class=Response)
def template_xlsx(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> Response:
    cur = users_repo.get_profile(db, p.user_id)["reporting_currency"]
    visible = [c for c in cats.list_categories(db, p.user_id) if not c["hidden"]]
    return Response(
        template.build_xlsx(visible, get_currency_registry().codes, cur),
        media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="finpilot-template.xlsx"'},
    )


@router.get("/batches", response_model=list[sc.ImportBatchOut])
def batches(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> list[dict]:
    return imp.list_batches(db, p.user_id)


def read_upload(file: UploadFile) -> bytes:
    content = file.file.read(P.MAX_BYTES + 1)
    if len(content) > P.MAX_BYTES:
        raise imp.ImportError_("File is larger than 5 MB.", 413)
    return content


@router.post("/upload", response_model=sc.ImportPreviewOut, status_code=status.HTTP_201_CREATED)
def upload(
    file: UploadFile = File(...),
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    content = read_upload(file)
    b = imp.create_batch(db, p.user_id, file.filename or "upload.csv", content)
    return imp.preview(db, p.user_id, b["id"], fx)


@router.get("/{batch_id}", response_model=sc.ImportPreviewOut)
def preview(
    batch_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    return imp.preview(db, p.user_id, batch_id, fx)


@router.patch("/{batch_id}", response_model=sc.ImportPreviewOut)
def remap(
    batch_id: uuid.UUID,
    body: sc.ImportMappingIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    imp.update_mapping(db, p.user_id, batch_id, body.mapping, body.options)
    return imp.preview(db, p.user_id, batch_id, fx)


@router.post(
    "/{batch_id}/confirm", response_model=sc.ImportBatchOut, status_code=status.HTTP_202_ACCEPTED
)
def confirm(batch_id: uuid.UUID, p: Principal = Depends(get_principal)) -> dict:
    # Own short transaction: the worker must see status=importing before it starts.
    with user_session(p.user_id) as s:
        imp.mark_importing(s, p.user_id, batch_id)
    run_import.delay(str(p.user_id), str(batch_id))
    with user_session(p.user_id) as s:
        return next(b for b in imp.list_batches(s, p.user_id, 100) if b["id"] == batch_id)


@router.post("/{batch_id}/undo", response_model=sc.Message)
def undo(
    batch_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    n = imp.undo(db, p.user_id, batch_id)
    return {"detail": f"Import undone: {n} transactions removed."}
