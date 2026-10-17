"""Dashboard data (deterministic aggregates in the reporting currency)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from finpilot.api import schemas as sc
from finpilot.api.routes_fx import fx_service
from finpilot.fx.service import FXService
from finpilot.insights import dashboard
from finpilot.tenancy import Principal, get_db, get_principal

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=sc.DashboardOut)
def get_dashboard(
    period: Literal["this_month", "30d", "3m", "6m", "12m"] = "this_month",
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
    fx: FXService = Depends(fx_service),
) -> dict:
    return dashboard.build(db, p.user_id, period, fx)
