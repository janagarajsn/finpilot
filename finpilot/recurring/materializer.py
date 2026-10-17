"""Materializes due occurrences of user-declared recurring rules into `transactions`.

Idempotent: the partial unique index (recurring_rule_id, date) plus a deterministic `hash`
make re-runs and concurrent ticks harmless (ON CONFLICT DO NOTHING). Catch-up: every due
date between `next_run_date` and the user's LOCAL today is created after downtime.
Rules are locked FOR UPDATE SKIP LOCKED so parallel workers split the work.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from finpilot.db.session import system_session, user_session
from finpilot.fx.service import FXService
from finpilot.recurring.rules import next_occurrence, occurrences_between
from finpilot.recurring.service import local_today, schedule_of
from finpilot.transactions import service as txns

log = logging.getLogger(__name__)


@dataclass
class MaterializeResult:
    rules: int = 0
    created: int = 0
    errors: int = 0


def due_users(now_utc: dt.datetime | None = None) -> list[uuid.UUID]:
    """Users with at least one active rule due by THEIR local date (system/admin role)."""
    now_utc = now_utc or dt.datetime.now(dt.UTC)
    with system_session("recurring: find due users") as s:
        rows = s.execute(
            text("""
            SELECT DISTINCT r.user_id FROM recurring_rules r JOIN users u ON u.id = r.user_id
            WHERE r.status = 'active' AND r.next_run_date IS NOT NULL
              AND r.next_run_date <= (CAST(:now AS timestamptz) AT TIME ZONE u.timezone)::date"""),
            {"now": now_utc},
        )
        return [r[0] for r in rows]


def materialize_user(
    user_id: uuid.UUID, fx: FXService | None, today: dt.date | None = None
) -> MaterializeResult:
    res = MaterializeResult()
    with user_session(user_id) as s:
        tz = s.execute(
            text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id}
        ).scalar_one()
        today = today or local_today(tz)
        rules = (
            s.execute(
                text("""SELECT * FROM recurring_rules WHERE user_id = :u AND status = 'active'
                                  AND next_run_date IS NOT NULL AND next_run_date <= :t
                                  FOR UPDATE SKIP LOCKED"""),
                {"u": user_id, "t": today},
            )
            .mappings()
            .all()
        )
        for r in rules:
            rule = dict(r)
            res.rules += 1
            sched = schedule_of(rule)
            dates = occurrences_between(sched, rule["next_run_date"], today)
            last_ok: dt.date | None = None
            failed_at: dt.date | None = None
            for d in dates:
                inp = txns.TxnInput(
                    kind=rule["kind"],
                    amount=rule["amount"],
                    date=d,
                    description=rule["description"],
                    currency=rule["currency"],
                    category_id=rule["category_id"],
                    account_id=rule["account_id"],
                    notes=rule["notes"],
                )
                try:
                    with s.begin_nested():
                        if (
                            txns.create(
                                s,
                                user_id,
                                inp,
                                fx,
                                source="recurring_rule",
                                hash_suffix=f"r:{rule['id']}:{d.isoformat()}",
                                recurring_rule_id=rule["id"],
                            )
                            is not None
                        ):
                            res.created += 1
                    last_ok = d
                except Exception:
                    # Stop here: the next tick retries from this date, so no occurrence is lost.
                    res.errors += 1
                    failed_at = d
                    log.exception("materialize failed rule=%s date=%s", rule["id"], d)
                    break
            nxt = failed_at or next_occurrence(sched, today + dt.timedelta(days=1))
            s.execute(
                text("""UPDATE recurring_rules SET last_materialized_date = coalesce(:last, last_materialized_date),
                              next_run_date = :n, status = CASE WHEN :n IS NULL THEN 'ended' ELSE status END
                              WHERE id = :id AND user_id = :u"""),
                {"last": last_ok, "n": nxt, "id": rule["id"], "u": user_id},
            )
    return res


def materialize_all(fx: FXService | None) -> MaterializeResult:
    total = MaterializeResult()
    for uid in due_users():
        try:
            r = materialize_user(uid, fx)
            total.rules += r.rules
            total.created += r.created
            total.errors += r.errors
        except Exception:
            total.errors += 1
            log.exception("materialize failed for user %s", uid)
    return total
