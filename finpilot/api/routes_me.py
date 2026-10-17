"""Profile & settings for the authenticated user (always scoped by the principal)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.api.cookies import clear_auth_cookies
from finpilot.api.deps import client_info
from finpilot.api.routes_fx import fx_service
from finpilot.api.routes_transactions import _enqueue_categorization
from finpilot.auth import service as auth
from finpilot.auth.service import validate_profile
from finpilot.db.repos import users as repo
from finpilot.fx.service import FXService
from finpilot.money import get_currency_registry
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(tags=["me"])


@router.get("/me", response_model=sc.ProfileOut)
def me(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict:
    return repo.get_profile(db, p.user_id)


@router.patch("/me", response_model=sc.ProfileOut)
def patch_me(
    body: sc.ProfilePatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    changes = body.model_dump(exclude_none=True)
    if "reporting_currency" in changes:
        changes["reporting_currency"] = changes["reporting_currency"].upper()
    validate_profile(changes.get("reporting_currency"), changes.get("timezone"))
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    return repo.update_profile(db, p.user_id, changes)


@router.get("/settings", response_model=sc.SettingsOut)
def get_settings_(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict:
    return repo.get_settings_row(db, p.user_id)


@router.patch("/settings", response_model=sc.SettingsOut)
def patch_settings(
    body: sc.SettingsPatch,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict:
    return repo.update_settings_row(db, p.user_id, body.model_dump(exclude_unset=True))


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str | None = Field(default=None, max_length=20)


@router.get("/me/sample-data")
def sample_datasets(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict:
    """The sample datasets on offer, and which one (if any) is loaded."""
    from finpilot.ingest import sample_data as sd

    return {"datasets": sd.datasets(), "loaded": sd.loaded_dataset(db, p.user_id)}


@router.post("/me/sample-data", status_code=201)
def sample_data(
    background: BackgroundTasks,
    body: SampleIn | None = None,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    """Load a 6-month sample history from data/sample/ (undoable from the Import page).
    `dataset`: inr | usd | mixed; default follows the reporting currency."""
    from finpilot.ingest.sample_data import load_sample

    out = load_sample(db, p.user_id, fx, body.dataset if body else None)
    background.add_task(_enqueue_categorization, p.user_id)
    return {k: str(v) if k == "batch_id" else v for k, v in out.items()}


@router.get("/currencies", response_model=list[sc.CurrencyOut], tags=["fx"])
def currencies() -> list[dict]:
    reg = get_currency_registry()
    return [
        reg.get(c).model_dump(include={"code", "name", "symbol", "locale", "minor_units"})
        for c in reg.codes
    ]


class DeleteMeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=200)


@router.get("/me/export")
def export_me(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> Response:
    """Everything FinPilot stores about you, as one JSON file (no password or token hashes)."""
    from finpilot.account_data import export_json

    name = f"finpilot-export-{dt.date.today().isoformat()}.json"
    return Response(
        export_json(db, p.user_id),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/me", status_code=204)
def delete_me(
    body: DeleteMeIn, request: Request, p: Principal = Depends(get_principal)
) -> Response:
    """Delete your account and all its data (password required). Cannot be undone."""
    from finpilot.account_data import delete_account

    try:
        delete_account(p.user_id, body.password, client_info(request))
    except auth.InvalidCredentials as e:
        raise HTTPException(403, str(e)) from e
    out = Response(status_code=204)
    clear_auth_cookies(out)
    return out
