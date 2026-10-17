"""FX endpoints. All numbers come from finpilot.fx.service (deterministic, Decimal)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query

from finpilot.api import schemas as sc
from finpilot.fx.service import FXService, FXUnavailable, build_fx_service
from finpilot.money import UnsupportedCurrency, get_currency_registry
from finpilot.tenancy import Principal, get_principal

router = APIRouter(prefix="/fx", tags=["fx"])


@lru_cache
def fx_service() -> FXService:
    return build_fx_service()


@router.get("/convert", response_model=sc.ConversionOut)
def convert(
    amount: str = Query(max_length=24),
    from_: str = Query(alias="from", max_length=3),
    to: str = Query(max_length=3),
    date: dt.date | None = None,
    _: Principal = Depends(get_principal),
    fx: FXService = Depends(fx_service),
) -> dict:
    try:
        amt = Decimal(amount.replace(",", ""))
        if not amt.is_finite() or abs(amt) >= Decimal("1e15"):
            raise InvalidOperation
    except InvalidOperation:
        raise HTTPException(422, "amount must be a number") from None
    try:
        return fx.convert(amt, from_, to, date).as_dict()
    except UnsupportedCurrency as e:
        raise HTTPException(422, str(e)) from None
    except FXUnavailable as e:
        raise HTTPException(503, str(e)) from None


@router.get("/rates", response_model=list[sc.RateOut])
def rates(
    date: dt.date | None = None,
    _: Principal = Depends(get_principal),
    fx: FXService = Depends(fx_service),
) -> list[dict]:
    reg = get_currency_registry()
    out = []
    for code in reg.codes:
        if code == reg.base:
            continue
        try:
            out.append(fx.rate_to_inr(code, date).as_dict())
        except FXUnavailable as e:
            raise HTTPException(503, str(e)) from None
    return out


@router.get("/status", response_model=sc.FXStatusOut)
def status(_: Principal = Depends(get_principal), fx: FXService = Depends(fx_service)) -> dict:
    return fx.status().as_dict()
