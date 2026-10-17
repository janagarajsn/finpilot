"""Beat TICK tasks for proactive features (no per-user cron entries):

- digests_due (15 min): users whose weekly local slot has come (and no digest for that period yet)
  get a pipeline run (trigger 'schedule'); its `deliver` node sends the digest from fresh data
- risk_alerts (hourly): low-balance alerts (forecast bad case < buffer within 14 days, 7-day dedupe)
- refresh_insights (daily): a pipeline run for every active user (+ answer pre-warming)
- goal_snapshots (weekly): a weekly progress point per goal even without data changes
- cache_gc (nightly): semantic-cache rows of old data versions, old logs, used link tokens, outbox
- telegram_poll (1 min, only with TELEGRAM_POLLING=true): bind chats without a public webhook
Synthetic (eval) users are always excluded.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from typing import Any

from sqlalchemy import text

from finpilot.db.session import system_session, user_session
from finpilot.digests.schedule import due_period
from finpilot.tasks.celery_app import app
from finpilot.tasks.locks import redis_lock

log = logging.getLogger(__name__)
ACTIVE_DAYS = 30
OUTBOX_KEEP_DAYS = 14


def _fx() -> Any:
    from finpilot.fx.service import build_fx_service

    return build_fx_service()


def due_users(now: dt.datetime | None = None) -> list[tuple[uuid.UUID, str]]:
    """(user, period_key) for every real user whose digest is due and not yet sent."""
    now = now or dt.datetime.now(dt.UTC)
    with system_session("digests_due") as s:
        rows = s.execute(
            text("""SELECT u.id, u.timezone, st.digest_dow, st.digest_hour
                    FROM users u JOIN user_settings st ON st.user_id = u.id
                    WHERE NOT u.is_synthetic AND st.digest_enabled""")
        ).all()
        out = []
        for uid, tz, dow, hour in rows:
            pk = due_period(now, tz, dow, hour)
            if pk is None:
                continue
            sent = s.execute(
                text(
                    """SELECT 1 FROM digests WHERE user_id = :u AND period_key = :p AND status = 'sent'"""
                ),
                {"u": uid, "p": pk},
            ).first()
            if not sent:
                out.append((uid, pk))
    return out


@app.task(name="finpilot.tasks.digests_due")
def digests_due() -> dict:
    from finpilot.tasks.pipeline_tasks import run_pipeline

    with redis_lock("beat:digests_due", ttl_s=840) as got:
        if not got:
            return {"skipped": 1}
        due = due_users()
        for uid, _ in due:
            run_pipeline.delay(str(uid), "schedule")
        return {"due": len(due)}


@app.task(name="finpilot.tasks.risk_alerts")
def risk_alerts() -> dict:
    from finpilot.digests.alerts import check_user

    with redis_lock("beat:risk_alerts", ttl_s=3300) as got:
        if not got:
            return {"skipped": 1}
        with system_session("risk_alerts") as s:
            users = [
                r[0]
                for r in s.execute(
                    text("""SELECT u.id FROM users u JOIN user_settings st ON st.user_id = u.id
                            WHERE NOT u.is_synthetic AND st.risk_alert_enabled
                              AND EXISTS (SELECT 1 FROM transactions t WHERE t.user_id = u.id)""")
                )
            ]
        fx, counts = _fx(), {"checked": 0, "raised": 0, "deduped": 0, "errors": 0}
        for uid in users:
            try:
                out = check_user(uid, fx)
            except Exception:
                log.warning("risk alert check failed for %s", uid, exc_info=True)
                counts["errors"] += 1
                continue
            counts["checked"] += 1
            if out["status"] in ("raised", "deduped"):
                counts[out["status"]] += 1
        return counts


def active_users(days: int = ACTIVE_DAYS) -> list[uuid.UUID]:
    """Logged in or changed data recently (real users only)."""
    with system_session("active_users") as s:
        return [
            r[0]
            for r in s.execute(
                text("""SELECT u.id FROM users u WHERE NOT u.is_synthetic AND (
                          EXISTS (SELECT 1 FROM auth_events e WHERE e.user_id = u.id AND e.kind IN ('login_ok','register','refresh')
                                  AND e.created_at > now() - make_interval(days => :d))
                          OR EXISTS (SELECT 1 FROM transactions t WHERE t.user_id = u.id
                                  AND t.created_at > now() - make_interval(days => :d)))"""),
                {"d": days},
            )
        ]


@app.task(name="finpilot.tasks.refresh_insights")
def refresh_insights() -> dict:
    from finpilot.tasks.pipeline_tasks import run_pipeline

    with redis_lock("beat:refresh_insights", ttl_s=3000) as got:
        if not got:
            return {"skipped": 1}
        users = active_users()
        for i, uid in enumerate(users):  # spread the load: one run every 2 s
            run_pipeline.apply_async((str(uid), "schedule"), countdown=min(i * 2, 3600))
        return {"users": len(users)}


@app.task(name="finpilot.tasks.goal_snapshots")
def goal_snapshots() -> dict:
    from finpilot.agents import goal_tracker
    from finpilot.forecasting.service import get_forecast

    with redis_lock("beat:goal_snapshots", ttl_s=3000) as got:
        if not got:
            return {"skipped": 1}
        with system_session("goal_snapshots") as s:
            users = [
                r[0]
                for r in s.execute(
                    text("""SELECT DISTINCT g.user_id FROM goals g JOIN users u ON u.id = g.user_id
                            WHERE NOT u.is_synthetic""")
                )
            ]
        fx, n = _fx(), 0
        for uid in users:
            try:
                with user_session(uid) as s:
                    n += len(goal_tracker.snapshot(s, uid, get_forecast(s, uid, fx=fx)))
            except Exception:
                log.warning("goal snapshot failed for %s", uid, exc_info=True)
        return {"users": len(users), "snapshots": n}


@app.task(name="finpilot.tasks.cache_gc")
def cache_gc() -> dict:
    """Nightly cleanup of rows nothing can reach any more (version-based invalidation leaves them)."""
    from finpilot.delivery.log_sink import OUTBOX

    with redis_lock("beat:cache_gc", ttl_s=3000) as got:
        if not got:
            return {"skipped": 1}
        out: dict[str, int] = {}
        with system_session("cache_gc") as s:
            for name, sql in (
                (
                    "semantic_cache",
                    """DELETE FROM semantic_cache c USING users u WHERE u.id = c.user_id
                       AND (c.data_version < u.data_version OR c.created_at < now() - interval '30 days')""",
                ),
                (
                    "answer_log",
                    "DELETE FROM answer_log WHERE created_at < now() - interval '90 days'",
                ),
                ("job_runs", "DELETE FROM job_runs WHERE started_at < now() - interval '30 days'"),
                (
                    "telegram_link_tokens",
                    "DELETE FROM telegram_link_tokens WHERE used_at IS NOT NULL OR expires_at < now() - interval '1 day'",
                ),
            ):
                out[name] = int(getattr(s.execute(text(sql)), "rowcount", 0) or 0)
        cutoff = time.time() - OUTBOX_KEEP_DAYS * 86400
        removed = 0
        if OUTBOX.exists():
            for f in OUTBOX.glob("*.json"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
        out["outbox_files"] = removed
        return out


@app.task(name="finpilot.tasks.telegram_poll")
def telegram_poll() -> dict:
    from finpilot.delivery.telegram import poll_once
    from finpilot.redis_client import get_redis

    with redis_lock("beat:telegram_poll", ttl_s=55) as got:
        if not got:
            return {"skipped": 1}
        return {"updates": poll_once(get_redis())}
