"""FX rate refresh: twice daily via beat, once at worker startup if rates are stale, and a
15-minute recheck while live rates are unavailable."""

from __future__ import annotations

import logging

from celery.signals import worker_ready

from finpilot.fx.service import FXUnavailable, build_fx_service
from finpilot.money import get_currency_registry
from finpilot.tasks.celery_app import app

log = logging.getLogger(__name__)


@app.task(
    name="finpilot.tasks.refresh_fx_rates",
    autoretry_for=(FXUnavailable,),
    retry_backoff=60,
    max_retries=3,
)
def refresh_fx_rates() -> dict[str, str]:
    fx = build_fx_service()
    reg = get_currency_registry()
    out: dict[str, str] = {}
    for code in reg.codes:
        if code == reg.base:
            continue
        r = fx.rate_to_inr(code)  # latest mode: fetches through MCP → HTTP when not fresh
        out[code] = f"{r.rate_inr_per_unit} @ {r.rate_date} ({r.source})"
    log.info("FX refreshed: %s", out)
    return out


@app.task(name="finpilot.tasks.fx_recheck")
def fx_recheck() -> dict[str, object]:
    """Every 15 min: only while rates are NOT live (outage), try the live routes again so the
    'live rates unavailable' banner clears soon after the FX server or source recovers."""
    fx = build_fx_service()
    st = fx.status()
    if st.live:
        return {"live": True, "probed": False}
    return {"live": fx.probe_live(), "probed": True}


@worker_ready.connect
def _refresh_on_startup(**_: object) -> None:
    try:
        st = build_fx_service().status()
        if st.as_of is None or st.stale or not st.live:
            refresh_fx_rates.delay()
    except Exception:
        log.warning("startup FX refresh check failed", exc_info=True)
