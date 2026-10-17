"""Celery application (broker = Redis DB 1, results = Redis DB 2). Beat runs TICK tasks."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from finpilot.config import get_settings

_s = get_settings()
app = Celery("finpilot", broker=_s.celery_broker_url, backend=_s.celery_result_backend)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
)
app.conf.include = [
    "finpilot.tasks.fx_tasks",
    "finpilot.tasks.ledger_tasks",
    "finpilot.tasks.eval_tasks",
    "finpilot.tasks.pipeline_tasks",
    "finpilot.tasks.memory_tasks",
    "finpilot.tasks.digest_tasks",
    "finpilot.tasks.jobs",
]
app.conf.task_always_eager = _s.celery_eager  # tests / single-process demos
app.conf.task_eager_propagates = True
app.conf.beat_schedule = {
    "heartbeat": {"task": "finpilot.tasks.heartbeat", "schedule": 900.0},
    # ECB reference rates publish ~16:00 CET; refresh after that and again early morning (UTC).
    "materialize-recurring": {
        "task": "finpilot.tasks.materialize_recurring_rules",
        "schedule": 900.0,
    },
    # nightly deterministic eval (no LLM cost); full LLM evals are run on demand by an admin
    "nightly-eval-offline": {
        "task": "finpilot.tasks.run_eval",
        "schedule": crontab(minute=15, hour=3),
        "kwargs": {"mode": "offline", "trigger": "scheduled"},
    },
    # chat sessions idle for 15 min → long-term memories (memory writer)
    "memory-consolidation": {"task": "finpilot.tasks.memory_consolidation", "schedule": 900.0},
    "refresh-fx-rates": {
        "task": "finpilot.tasks.refresh_fx_rates",
        "schedule": crontab(minute=30, hour="2,16"),
    },
    # outage recovery: probes the live FX routes only while rates are not live
    "fx-recheck": {"task": "finpilot.tasks.fx_recheck", "schedule": 900.0},
    # proactive delivery (Phase 10): ticks select the users; nothing is scheduled per user
    "digests-due": {"task": "finpilot.tasks.digests_due", "schedule": 900.0},
    "risk-alerts": {"task": "finpilot.tasks.risk_alerts", "schedule": crontab(minute=5)},
    "refresh-insights": {
        "task": "finpilot.tasks.refresh_insights",
        "schedule": crontab(minute=0, hour=1),
    },
    "goal-snapshots": {
        "task": "finpilot.tasks.goal_snapshots",
        "schedule": crontab(minute=30, hour=1, day_of_week="mon"),
    },
    "cache-gc": {"task": "finpilot.tasks.cache_gc", "schedule": crontab(minute=45, hour=3)},
}
if _s.telegram_polling:  # no public webhook URL (local dev): poll the Bot API instead
    app.conf.beat_schedule["telegram-poll"] = {
        "task": "finpilot.tasks.telegram_poll",
        "schedule": 60.0,
    }


@app.task(name="finpilot.tasks.heartbeat")
def heartbeat() -> str:
    return "ok"


# job_runs bookkeeping (Celery signals) must be connected in every process that runs tasks,
# including the API process in eager mode, not only in workers that load `include`.
import finpilot.tasks.jobs  # noqa: E402, F401
