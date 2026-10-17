"""Coordinator jobs: run the pipeline for one user (one at a time per user), debounce bursts of
writes into a single run, and resume runs a dead worker left behind."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from celery.signals import worker_ready
from sqlalchemy import text

from finpilot.db.session import system_session
from finpilot.fx.service import build_fx_service
from finpilot.redis_client import get_redis
from finpilot.tasks.celery_app import app
from finpilot.tasks.locks import redis_lock

log = logging.getLogger(__name__)
DEBOUNCE_S = 90  # a burst of edits → one pipeline run 90 s after the first
STALE_AFTER = dt.timedelta(minutes=15)


def _llm():
    from finpilot.llm.client import get_llm

    return get_llm()


@app.task(name="finpilot.tasks.run_pipeline", acks_late=True)
def run_pipeline(user_id: str, trigger: str = "manual", run_id: str | None = None) -> dict:
    """Run (or, given a created run id, execute) the coordinator for one user."""
    from finpilot.agents import coordinator

    uid = uuid.UUID(user_id)
    with redis_lock(f"pipeline:{uid}", ttl_s=900) as got:
        if not got:
            # a created run that cannot start now: record why instead of leaving it 'running'
            if run_id:
                coordinator._finish(
                    uid, run_id, None, "another pipeline run is in progress for this user", None
                )
            return {"skipped": 1}
        get_redis().delete(f"pipeline:pending:{uid}")
        rid = run_id or coordinator.create_run(uid, trigger)
        out = coordinator.run(uid, rid, trigger=trigger, llm=_llm(), fx=build_fx_service())
        return {"run_id": rid, "status": out["status"], "degraded": out["degraded"]}


@app.task(name="finpilot.tasks.prewarm_answers")
def prewarm_answers(user_id: str) -> dict:
    """At login: re-compute the dashboard chip answers if the pre-warmed ones are stale."""
    from finpilot.answers import service as answers
    from finpilot.db.session import user_session

    uid = uuid.UUID(user_id)
    with redis_lock(f"prewarm:{uid}", ttl_s=300) as got:
        if not got:
            return {"skipped": 1}
        with user_session(uid) as s:
            if not answers.is_stale(s, uid):
                return {"fresh": 1}
            return answers.prewarm(s, uid, _llm(), build_fx_service())


def schedule_prewarm(user_id: uuid.UUID | str) -> None:
    try:
        prewarm_answers.delay(str(user_id))
    except Exception:  # broker down: the chips compute on demand
        log.warning("could not enqueue answer prewarm for %s", user_id, exc_info=True)


def schedule_pipeline(user_id: uuid.UUID | str, trigger: str = "write") -> bool:
    """Debounced: at most one pending run per user. Returns True if a run was enqueued."""
    try:
        if not get_redis().set(f"pipeline:pending:{user_id}", "1", nx=True, ex=DEBOUNCE_S + 60):
            return False
        if app.conf.task_always_eager:
            run_pipeline.delay(str(user_id), trigger)
        else:
            run_pipeline.apply_async((str(user_id), trigger), countdown=DEBOUNCE_S)
        return True
    except Exception:  # broker down: the next write or the daily refresh will catch up
        log.warning("could not schedule pipeline for %s", user_id, exc_info=True)
        return False


@app.task(name="finpilot.tasks.resume_stale_runs")
def resume_stale_runs() -> dict:
    """Runs still 'running' long after they started belong to a dead worker: resume them from
    their last checkpoint (or mark them failed when there is no checkpoint)."""
    from finpilot.agents import coordinator

    with system_session("pipeline: find stale runs") as s:
        rows = s.execute(
            text(
                "SELECT id, user_id FROM pipeline_runs WHERE status = 'running' AND started_at < now() - make_interval(mins => :m)"
            ),
            {"m": int(STALE_AFTER.total_seconds() // 60)},
        ).all()
    resumed = failed = 0
    for rid, uid in rows:
        with redis_lock(f"pipeline:{uid}", ttl_s=900) as got:
            if not got:
                continue
            try:
                coordinator.resume_run(uid, str(rid), llm=_llm(), fx=build_fx_service())
                resumed += 1
            except coordinator.PipelineError as e:
                coordinator._finish(uid, str(rid), None, f"abandoned: {e}", None)
                failed += 1
    return {"resumed": resumed, "failed": failed}


@worker_ready.connect
def _resume_on_start(**_: object) -> None:
    try:
        resume_stale_runs.delay()
    except Exception:
        log.warning("could not enqueue resume_stale_runs", exc_info=True)
