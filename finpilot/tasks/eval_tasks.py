"""Evaluation runs as Celery tasks (admin button, nightly schedule)."""

from __future__ import annotations

import logging

from finpilot.tasks.celery_app import app
from finpilot.tasks.locks import redis_lock

log = logging.getLogger(__name__)


@app.task(name="finpilot.tasks.run_eval", time_limit=3600)
def run_eval(mode: str = "offline", trigger: str = "manual") -> dict:
    from eval.runner import run

    with redis_lock("evalrun", ttl_s=3600) as got:
        if not got:
            return {"skipped": "another evaluation is running"}
        run_id, code = run(mode, trigger=trigger)
        return {"run_id": str(run_id), "gate_failed": bool(code)}
