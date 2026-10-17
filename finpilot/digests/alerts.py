"""Proactive risk alerts: the forecast's bad case (p10) dips below the user's cash buffer within
the next 14 days. One alert per 7 days (same dedupe_key), delivered on the digest channel and
always stored in-app (Inbox + a dashboard banner)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.db.session import user_session
from finpilot.digests.service import deliver
from finpilot.money import quantize
from finpilot.templating import render_snippet

log = logging.getLogger(__name__)
WINDOW_DAYS = 14
DEDUPE = dt.timedelta(days=7)


def low_balance(
    s: Session, uid: uuid.UUID, fx: Any, window_days: int = WINDOW_DAYS
) -> dict[str, Any] | None:
    """The first day within `window_days` where the bad case is below the buffer, and the low."""
    from finpilot.forecasting.service import get_forecast

    fc = get_forecast(s, uid, fx=fx)
    end = fc.as_of + dt.timedelta(days=window_days)
    days = [(d, p) for d, p in zip(fc.dates, fc.p10, strict=True) if d <= end]
    below = [(d, p) for d, p in days if Decimal(str(p)) < fc.buffer]
    if not below:
        return None
    low_d, low = min(days, key=lambda x: x[1])
    bills = [
        x for x in fc.scheduled if x.amount < 0 and dt.date.fromisoformat(x.date) <= below[0][0]
    ][:3]
    return {
        "currency": fc.currency,
        "buffer": fc.buffer,
        "first_below": below[0][0],
        "low": quantize(Decimal(str(low)), fc.currency),
        "low_date": low_d,
        "forecast_id": fc.id,
        "bills": [{"date": b.date, "label": b.label, "amount": -b.amount} for b in bills],
    }


def check_user(uid: uuid.UUID, fx: Any, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    with user_session(uid) as s:
        st = (
            s.execute(
                text(
                    "SELECT risk_alert_enabled, digest_channel FROM user_settings WHERE user_id = :u"
                ),
                {"u": uid},
            )
            .mappings()
            .first()
        )
        if st is None or not st["risk_alert_enabled"]:
            return {"status": "disabled"}
        hit = low_balance(s, uid, fx)
        if hit is None:
            return {"status": "ok"}
        key = "low_balance"
        recent = s.execute(
            text(
                """SELECT 1 FROM alerts WHERE user_id = :u AND dedupe_key = :k AND created_at > :since LIMIT 1"""
            ),
            {"u": uid, "k": key, "since": now - DEDUPE},
        ).first()
        if recent:
            return {"status": "deduped"}
        cur = hit["currency"]
        ctx = {
            "buffer": hit["buffer"],
            "cur": cur,
            "date": hit["first_below"].strftime("%d %b"),
            "low": hit["low"],
            "low_date": hit["low_date"].strftime("%d %b"),
            "bills": ", ".join(f"{b['label']} ({b['date']})" for b in hit["bills"]),
        }
        title = render_snippet("digest", "alert.low_balance.title", **ctx)
        body = render_snippet("digest", "alert.low_balance.body", **ctx)
        severity = "critical" if hit["low"] < 0 else "warning"
        aid = s.execute(
            text("""INSERT INTO alerts (user_id, kind, dedupe_key, severity, title, body, payload_json, channel)
                    VALUES (:u, 'low_balance', :k, :sev, :t, :b, CAST(:p AS jsonb), :c) RETURNING id"""),
            {
                "u": uid,
                "k": key,
                "sev": severity,
                "t": title[:200],
                "b": body[:2000],
                "p": json.dumps(hit, default=str),
                "c": st["digest_channel"],
            },
        ).scalar_one()
        try:
            via, _ = deliver(s, uid, st["digest_channel"], title, body)
        except Exception:  # the in-app alert stands even if the channel fails
            log.warning("alert delivery failed for %s", uid, exc_info=True)
            via = "in_app"
        s.execute(
            text("UPDATE alerts SET delivered_via = :v WHERE id = :i AND user_id = :u"),
            {"v": via, "i": aid, "u": uid},
        )
    return {"status": "raised", "id": str(aid), "severity": severity, "via": via}


def list_alerts(
    s: Session, uid: uuid.UUID, unread_only: bool = False, limit: int = 30
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in s.execute(
            text(f"""SELECT id, kind, severity, title, body, channel, delivered_via, read_at, created_at
                    FROM alerts WHERE user_id = :u {"AND read_at IS NULL" if unread_only else ""}
                    ORDER BY created_at DESC LIMIT :n"""),  # noqa: S608 — fixed clause choice
            {"u": uid, "n": limit},
        ).mappings()
    ]


def mark_read(s: Session, uid: uuid.UUID, aid: uuid.UUID) -> bool:
    from finpilot.db.session import rowcount

    return bool(
        rowcount(
            s.execute(
                text(
                    "UPDATE alerts SET read_at = coalesce(read_at, now()) WHERE id = :i AND user_id = :u"
                ),
                {"i": aid, "u": uid},
            )
        )
    )
