from __future__ import annotations

from finpilot.tasks.celery_app import app


def test_beat_schedule_has_fx_refresh_twice_daily():
    import finpilot.tasks.fx_tasks  # noqa: F401 — registers the task

    entry = app.conf.beat_schedule["refresh-fx-rates"]
    assert entry["task"] == "finpilot.tasks.refresh_fx_rates"
    assert entry["schedule"].hour == {2, 16}
    assert "finpilot.tasks.refresh_fx_rates" in app.tasks


def test_recurring_tick_scheduled_every_15_minutes():
    import finpilot.tasks.ledger_tasks  # noqa: F401

    entry = app.conf.beat_schedule["materialize-recurring"]
    assert entry["schedule"] == 900.0 and "finpilot.tasks.materialize_recurring_rules" in app.tasks
