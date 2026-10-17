"""Proactive delivery: digest history, alerts, Telegram linking + webhook, scheduler evidence."""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.digests import alerts as alerts_mod
from finpilot.digests import service as digests
from finpilot.llm.client import LLMClient
from finpilot.tenancy import Principal, get_db, get_principal, require_admin

router = APIRouter(tags=["delivery"])


@router.get("/digests")
def list_digests(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> list[dict[str, Any]]:
    """Your digest history (newest first)."""
    return digests.list_digests(db, p.user_id)


@router.get("/digests/{digest_id}")
def get_digest(
    digest_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    d = digests.get_digest(db, p.user_id, digest_id)
    if d is None:
        raise HTTPException(404, "Digest not found.")
    return d


@router.post("/digest/preview")
def preview(
    p: Principal = Depends(require_admin),
    llm: LLMClient = Depends(llm_client),
    fx: Any = Depends(fx_service),
) -> dict[str, Any]:
    """Admin demo: build YOUR digest now, stored as a labelled preview (in-app only, not sent)."""
    key = "pv-" + digests.now_utc().strftime("%Y%m%d%H%M%S")
    return digests.send_digest(p.user_id, key, llm=llm, fx=fx, preview=True)


@router.get("/alerts")
def list_alerts(
    unread: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    return alerts_mod.list_alerts(db, p.user_id, unread_only=unread)


@router.post("/alerts/{alert_id}/read")
def read_alert(
    alert_id: uuid.UUID,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    if not alerts_mod.mark_read(db, p.user_id, alert_id):
        raise HTTPException(404, "Alert not found.")
    return {"id": str(alert_id), "read": True}


@router.post("/settings/telegram/link")
def telegram_link(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> dict[str, Any]:
    """A single-use deep link (t.me/<bot>?start=<token>) that binds your Telegram chat."""
    from finpilot.delivery.telegram import create_link

    return create_link(db, p.user_id)


@router.delete("/settings/telegram", status_code=204)
def telegram_unlink(
    p: Principal = Depends(get_principal), db: Session = Depends(get_db, scope="function")
) -> None:
    db.execute(
        text("""UPDATE user_settings SET telegram_chat_id = NULL,
                  digest_channel = CASE WHEN digest_channel = 'telegram' THEN 'in_app' ELSE digest_channel END
                WHERE user_id = :u"""),
        {"u": p.user_id},
    )


@router.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(secret: str, request: Request) -> dict[str, str]:
    """Bot API updates. The path secret is derived from SECRET_KEY; anything else is a 404."""
    from starlette.concurrency import run_in_threadpool

    from finpilot.delivery.telegram import handle_update, webhook_secret

    if not hmac.compare_digest(secret, webhook_secret()):
        raise HTTPException(404, "Not found")
    try:
        update = await request.json()
    except ValueError as e:
        raise HTTPException(400, "Invalid update") from e
    if not isinstance(update, dict):
        raise HTTPException(400, "Invalid update")
    return {"result": await run_in_threadpool(handle_update, update)}


@router.get("/scheduler/jobs")
def scheduler_jobs(_: Principal = Depends(require_admin)) -> list[dict[str, Any]]:
    """Beat schedule entries with last/next run and recent results (proves a real scheduler)."""
    from finpilot.tasks.jobs import status

    return status()
