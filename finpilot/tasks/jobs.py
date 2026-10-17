"""Scheduler evidence: every FinPilot task execution is recorded in `job_runs` (Celery signals),
and `status()` joins it with the beat schedule (last run, next run, recent results) for
`GET /scheduler/jobs`."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any

from celery.signals import task_failure, task_postrun, task_prerun
from sqlalchemy import text

from finpilot.db.session import system_session

log = logging.getLogger(__name__)
_started: dict[str, float] = {}


def _record(sql: str, params: dict[str, Any]) -> None:
    try:
        with system_session("job_runs") as s:
            s.execute(text(sql), params)
    except Exception:  # bookkeeping must never break a task
        log.debug("job_runs write failed", exc_info=True)


@task_prerun.connect
def _on_start(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    if not task or not str(task.name).startswith("finpilot."):
        return
    _started[str(task_id)] = time.perf_counter()
    _record(
        """INSERT INTO job_runs (task, task_id, status) VALUES (:t, :i, 'running')
           ON CONFLICT (task_id) WHERE task_id IS NOT NULL DO NOTHING""",
        {"t": task.name, "i": task_id},
    )


@task_postrun.connect
def _on_end(
    task_id: str | None = None,
    task: Any = None,
    retval: Any = None,
    state: str | None = None,
    **_: Any,
) -> None:
    if not task or not str(task.name).startswith("finpilot."):
        return
    ms = int((time.perf_counter() - _started.pop(str(task_id), time.perf_counter())) * 1000)
    ok = state in (None, "SUCCESS")
    _record(
        """UPDATE job_runs SET status = :st, finished_at = now(), duration_ms = :ms,
                  result_json = CAST(:r AS jsonb) WHERE task_id = :i AND status = 'running'""",
        {
            "st": "succeeded" if ok else "failed",
            "ms": ms,
            "r": json.dumps(
                retval if isinstance(retval, dict | list | str | int | float) else None, default=str
            ),
            "i": task_id,
        },
    )


@task_failure.connect
def _on_failure(
    task_id: str | None = None, exception: BaseException | None = None, sender: Any = None, **_: Any
) -> None:
    if not sender or not str(sender.name).startswith("finpilot."):
        return
    _record(
        "UPDATE job_runs SET status = 'failed', error = :e, finished_at = now() WHERE task_id = :i",
        {
            "e": f"{type(exception).__name__}: {exception}"[:500] if exception else "failed",
            "i": task_id,
        },
    )


def _describe(schedule: Any) -> str:
    if isinstance(schedule, int | float):
        mins = schedule / 60
        return f"every {int(mins)} min" if mins < 60 else f"every {mins / 60:g} h"
    return "cron " + str(schedule).replace("<crontab: ", "").split(" (")[0].rstrip(">") + " UTC"


def status(recent: int = 5) -> list[dict[str, Any]]:
    from celery.schedules import schedule as interval

    from finpilot.tasks.celery_app import app

    now = dt.datetime.now(dt.UTC)
    out = []
    with system_session("scheduler_status") as s:
        for name, entry in sorted(app.conf.beat_schedule.items()):
            task = entry["task"]
            runs = [
                dict(r)
                for r in s.execute(
                    text("""SELECT status, started_at, finished_at, duration_ms, result_json, error
                            FROM job_runs WHERE task = :t ORDER BY started_at DESC LIMIT :n"""),
                    {"t": task, "n": recent},
                ).mappings()
            ]
            sched = entry["schedule"]
            obj = interval(dt.timedelta(seconds=sched)) if isinstance(sched, int | float) else sched
            last = runs[0]["started_at"] if runs else None
            try:
                nxt = now + obj.remaining_estimate(last or now)
            except Exception:
                nxt = None
            out.append(
                {
                    "name": name,
                    "task": task,
                    "schedule": _describe(sched),
                    "last_run": last,
                    "last_status": runs[0]["status"] if runs else None,
                    "next_run": nxt,
                    "recent": runs,
                }
            )
    return out
