"""Ledger background jobs: recurring materialization tick and file imports."""

from __future__ import annotations

import logging
import uuid

from finpilot.db.session import user_session
from finpilot.fx.service import build_fx_service
from finpilot.ingest import import_service as imp
from finpilot.recurring.materializer import materialize_all
from finpilot.tasks.celery_app import app
from finpilot.tasks.locks import redis_lock

log = logging.getLogger(__name__)


@app.task(name="finpilot.tasks.materialize_recurring_rules")
def materialize_recurring_rules() -> dict[str, int]:
    with redis_lock("beat:materialize_recurring", ttl_s=14 * 60) as got:
        if not got:
            return {"skipped": 1}
        res = materialize_all(build_fx_service())
        log.info(
            "recurring tick: rules=%d created=%d errors=%d", res.rules, res.created, res.errors
        )
        return {"rules": res.rules, "created": res.created, "errors": res.errors}


@app.task(name="finpilot.tasks.run_import", acks_late=True)
def run_import(user_id: str, batch_id: str) -> dict[str, int]:
    uid, bid = uuid.UUID(user_id), uuid.UUID(batch_id)
    try:
        with user_session(uid) as s:
            counts = imp.run_import(s, uid, bid, build_fx_service())
    except Exception as e:
        log.exception("import %s failed", batch_id)
        with user_session(uid) as s:
            imp.mark_failed(s, uid, bid, f"{type(e).__name__}: {e}")
        raise
    from finpilot.tasks.pipeline_tasks import run_pipeline

    run_pipeline.delay(
        user_id, "import"
    )  # the pipeline categorizes first, then analyzes and advises
    return counts


@app.task(name="finpilot.tasks.categorize_pending")
def categorize_pending(user_id: str) -> dict:
    """LLM pass over the user's uncategorized rows (rules/memory already ran on write)."""
    from finpilot.agents.categorizer import Categorizer

    uid = uuid.UUID(user_id)
    with redis_lock(f"cat:{uid}", ttl_s=600) as got:
        if not got:
            return {"skipped": 1}
        with user_session(uid) as s:
            rep = Categorizer().run(s, uid)
        log.info("categorized user=%s %s", uid, rep.as_dict())
    from finpilot.tasks.pipeline_tasks import schedule_pipeline

    schedule_pipeline(
        user_id, "write"
    )  # categories changed → refresh insights/forecast/advice (debounced)
    return rep.as_dict()
