"""Weekly digest: compose from deterministic sources, phrase two sentences with the fast LLM
(grounded), deliver on the user's channel, and record it once per period.

Content (BUILD_PROMPT §Scheduler): ONE key insight, ONE action with its simulated effect, a goal
status line with the change since last week, notable bills in the next 14 days, and notices when
the last refresh was degraded or exchange rates are not live. Only the insight/action sentences
may be LLM-written (`prompts/digest/phrase.v1.md`); every number in them must pass the grounding
guard, else the template wording is used. Everything else is a template.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.config import get_settings
from finpilot.db.session import user_session
from finpilot.digests.schedule import period_label
from finpilot.llm.client import LLMClient
from finpilot.llm.schemas import DigestPhrase
from finpilot.money import get_currency_registry
from finpilot.observability.langfuse import agent_span
from finpilot.redis_client import get_redis
from finpilot.templating import render_message, render_snippet
from finpilot.trail.grounding import FactSet, extract, guarded_json

log = logging.getLogger(__name__)
PROMPT = "digest/phrase"


@dataclass
class Composed:
    subject: str
    body: str
    payload: dict[str, Any]
    generated_by: str = "template"
    grounding: dict[str, Any] | None = None
    degraded: bool = False
    notes: list[str] = field(default_factory=list)


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


def _top_action(s: Session, uid: uuid.UUID) -> dict[str, Any] | None:
    row = (
        s.execute(
            text("""SELECT r.id, r.title, r.action, r.est_monthly_impact, r.est_total_impact, t.forecast_ref_json
                    FROM recommendations r LEFT JOIN decision_trail t ON t.recommendation_id = r.id
                    WHERE r.user_id = :u AND r.status = 'proposed'
                      AND r.run_id = (SELECT run_id FROM recommendations WHERE user_id = :u ORDER BY created_at DESC LIMIT 1)
                    ORDER BY r.priority LIMIT 1"""),
            {"u": uid},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    ref = row["forecast_ref_json"] or {}
    sim = ref.get("simulation") or {}
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "action": row["action"],
        "monthly": row["est_monthly_impact"] or None,
        "end_change": Decimal(str(sim["end_change"]))
        if sim.get("end_change") is not None
        else None,
        "horizon_days": ref.get("horizon_days") or sim.get("horizon_days") or 90,
        "verdict_before": sim.get("verdict_before"),
        "verdict_after": sim.get("verdict_after"),
    }


def compose(
    s: Session,
    uid: uuid.UUID,
    *,
    llm: LLMClient | None,
    fx: Any,
    period_key: str,
    degraded: bool = False,
    preview: bool = False,
) -> Composed:
    from finpilot.agents import goal_tracker
    from finpilot.agents.analyzer import get_report
    from finpilot.forecasting.service import get_forecast
    from finpilot.insights import dashboard as dash

    user = (
        s.execute(text("SELECT name, reporting_currency FROM users WHERE id = :u"), {"u": uid})
        .mappings()
        .one()
    )
    cur = user["reporting_currency"]
    report = get_report(s, uid)
    top = dash.key_insights(report, 1)
    insight = {"title": top[0].title, "body": top[0].body} if top else None
    action = _top_action(s, uid)
    fc = get_forecast(s, uid, fx=fx)
    goals = goal_tracker.snapshot(s, uid, fc)  # this week's point (idempotent) + delta vs last week
    upcoming = dash.build(s, uid, "30d", fx)["upcoming"][:6]

    # ---------------- deterministic lines
    insight_t = (
        render_snippet("digest", "insight", **insight)
        if insight
        else render_snippet("digest", "no_insight")
    )
    action_t = (
        render_snippet("digest", "action", **action, cur=cur)
        if action
        else render_snippet("digest", "no_action")
    )
    goal_lines = [
        render_snippet(
            "digest",
            "goal",
            name=g["name"],
            status=g["status"],
            pct=g["pct"],
            eta=g["eta"],
            change=(g["delta"] or {}).get("progress_change") or None,
            cur=cur,
        )
        for g in goals
    ]
    bill_lines = [
        render_snippet(
            "digest",
            "bill",
            date=u["date"].strftime("%d %b"),
            description=u["description"],
            amount=u["amount"],
            currency=u["currency"],
            estimated=bool(u.get("estimated")),
        )
        for u in upcoming
    ]
    notices = []
    if degraded:
        notices.append(render_snippet("digest", "degraded"))
    if fx is not None:
        try:
            st = fx.status()
            if not st.live:
                notices.append(
                    render_snippet(
                        "digest",
                        "fx_notice",
                        approximate=st.approximate,
                        as_of=st.as_of or "unknown",
                    )
                )
        except Exception:
            log.debug("digest: fx status unavailable", exc_info=True)

    # ---------------- LLM phrasing of the two sentences (grounded; template fallback)
    generated_by, grounding = "template", None
    if (insight or action) and llm is not None and llm.available and not degraded:
        facts_obj = _jsonable({"currency": cur, "insight": insight, "action": action})
        facts = FactSet().add_obj({"currency": cur, "insight": insight, "action": action}, cur)
        # the insight/action sentences are deterministic renderings of computed facts: their
        # numbers, amounts and dates are facts too
        for t in (insight or {}).values():
            facts_from_text(facts, str(t))
        for k in ("title", "action"):
            facts_from_text(facts, str((action or {}).get(k) or ""))
        out, guard = guarded_json(
            llm,
            PROMPT,
            {
                "currency": cur,
                "symbol": get_currency_registry().get(cur).symbol,
                "facts": json.dumps(facts_obj, indent=1, ensure_ascii=False),
            },
            DigestPhrase,
            facts=facts,
            texts=lambda r: [r.insight, r.action],
            fallback=lambda: DigestPhrase(insight=insight_t, action=action_t),
            agent="digest",
            user_id=uid,
        )
        grounding = guard.as_dict()
        if not guard.fallback_used:
            insight_t = out.insight if insight else insight_t
            action_t = out.action if action else action_t
            generated_by = "llm"

    label = period_label(period_key) if not period_key.startswith("pv-") else "preview"
    body = render_message(
        "digest.txt.j2",
        name=user["name"] or "there",
        period_label=label,
        preview=preview,
        insight=insight_t,
        action=action_t,
        goals=goal_lines,
        bills=bill_lines,
        notices=notices,
        app_url=get_settings().app_base_url.rstrip("/"),
    ).strip()
    subject = render_snippet("digest", "subject", period_label=label)
    if preview:
        subject = "[Preview] " + subject
    payload = _jsonable(
        {
            "currency": cur,
            "insight": insight,
            "insight_text": insight_t,
            "action": action,
            "action_text": action_t,
            "goals": goal_lines,
            "bills": bill_lines,
            "notices": notices,
            "forecast": {
                "verdict": fc.outcome.verdict,
                "min_p10": fc.outcome.min_p10,
                "min_p10_date": fc.outcome.min_p10_date,
            },
        }
    )
    return Composed(subject, body, payload, generated_by, grounding, degraded)


def facts_from_text(facts: FactSet, text_: str) -> None:
    """Register every figure in a code-rendered sentence as a fact."""
    for m in extract(text_):
        if m.kind == "money" and m.value is not None and m.currency:
            facts.add_money(m.value, m.currency)
        elif m.kind in ("number", "percent") and m.value is not None:
            facts.add_number(m.value)
        elif m.kind in ("date", "month") and m.date and m.date[0]:
            facts.add_date(dt.date(m.date[0], m.date[1], m.date[2] or 1))


# ---------------------------------------------------------------------------- delivery
def deliver(
    s: Session, uid: uuid.UUID, channel: str, subject: str, body: str
) -> tuple[str, list[str]]:
    """Send on the user's channel. Returns (delivered_via, notes). In-app needs no sending."""
    from finpilot.delivery.email import send_email
    from finpilot.delivery.telegram import send_telegram

    if channel == "email":
        email = s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": uid}).scalar_one()
        return send_email(str(email), subject, body), []
    if channel == "telegram":
        chat = s.execute(
            text("SELECT telegram_chat_id FROM user_settings WHERE user_id = :u"), {"u": uid}
        ).scalar()
        if not chat:
            return "in_app", [render_snippet("digest", "telegram_fallback")]
        return send_telegram(str(chat), f"{subject}\n\n{body}", subject), []
    return "in_app", []


def send_digest(
    uid: uuid.UUID,
    period_key: str,
    *,
    llm: LLMClient | None,
    fx: Any,
    degraded: bool = False,
    run_id: str | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    """Compose and deliver one digest for `period_key` exactly once (UNIQUE row + Redis lock).
    A preview is stored with its own key and status 'preview' and only shown in the app."""
    r = get_redis()
    lock = f"lock:digest:{uid}:{period_key}"
    if not r.set(lock, "1", nx=True, ex=600):
        return {"status": "locked"}
    try:
        with user_session(uid) as s:
            pref = s.execute(
                text("SELECT digest_channel FROM user_settings WHERE user_id = :u"), {"u": uid}
            ).scalar()
            channel = "in_app" if preview else str(pref or "in_app")
            row = s.execute(
                text("""INSERT INTO digests (user_id, period_key, channel, status, run_id)
                        VALUES (:u, :p, :c, :st, :run)
                        ON CONFLICT (user_id, period_key) DO UPDATE SET channel = EXCLUDED.channel
                          WHERE digests.status IN ('pending', 'failed')
                        RETURNING id"""),
                {
                    "u": uid,
                    "p": period_key,
                    "c": channel,
                    "st": "preview" if preview else "pending",
                    "run": run_id,
                },
            ).scalar()
        if row is None:
            return {"status": "already_sent", "period_key": period_key}
        with (
            user_session(uid) as s,
            agent_span("digest", user_id=uid, metadata={"period": period_key}),
        ):
            c = compose(
                s, uid, llm=llm, fx=fx, period_key=period_key, degraded=degraded, preview=preview
            )
        try:
            with user_session(uid) as s:
                via, notes = (
                    ("in_app", []) if preview else deliver(s, uid, channel, c.subject, c.body)
                )
                s.execute(
                    text("""UPDATE digests SET status = :st, delivered_via = :via, subject = :sub, body_text = :b,
                                   payload_json = CAST(:p AS jsonb), degraded = :d, generated_by = :g,
                                   grounding_json = CAST(:gr AS jsonb), delivered_at = now(), error = NULL
                            WHERE id = :id AND user_id = :u"""),
                    {
                        "st": "preview" if preview else "sent",
                        "via": via,
                        "sub": c.subject[:200],
                        "b": c.body[:8000],
                        "p": json.dumps(c.payload | {"delivery_notes": notes}),
                        "d": c.degraded,
                        "g": c.generated_by,
                        "gr": json.dumps(c.grounding) if c.grounding else None,
                        "id": row,
                        "u": uid,
                    },
                )
        except Exception as e:  # delivery failed: keep the content, retry on the next tick
            log.warning("digest delivery failed for %s", uid, exc_info=True)
            with user_session(uid) as s:
                s.execute(
                    text("""UPDATE digests SET status = 'failed', error = :e, subject = :sub, body_text = :b
                            WHERE id = :id AND user_id = :u"""),
                    {
                        "e": f"{type(e).__name__}: {e}"[:500],
                        "sub": c.subject[:200],
                        "b": c.body[:8000],
                        "id": row,
                        "u": uid,
                    },
                )
            return {"status": "failed", "id": str(row), "error": type(e).__name__}
        return {
            "status": "preview" if preview else "sent",
            "id": str(row),
            "via": via,
            "generated_by": c.generated_by,
        }
    finally:
        r.delete(lock)


# ---------------------------------------------------------------------------- reads
def list_digests(s: Session, uid: uuid.UUID, limit: int = 30) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in s.execute(
            text("""SELECT id, period_key, channel, status, delivered_via, subject, degraded, generated_by,
                           read_at, created_at, delivered_at
                    FROM digests WHERE user_id = :u ORDER BY created_at DESC LIMIT :n"""),
            {"u": uid, "n": limit},
        ).mappings()
    ]


def get_digest(
    s: Session, uid: uuid.UUID, did: uuid.UUID, mark_read: bool = True
) -> dict[str, Any] | None:
    row = (
        s.execute(
            text("SELECT * FROM digests WHERE id = :i AND user_id = :u"), {"i": did, "u": uid}
        )
        .mappings()
        .first()
    )
    if row and mark_read and row["read_at"] is None:
        s.execute(
            text("UPDATE digests SET read_at = now() WHERE id = :i AND user_id = :u"),
            {"i": did, "u": uid},
        )
    return dict(row) if row else None


def unread_counts(s: Session, uid: uuid.UUID) -> dict[str, int]:
    d, a = s.execute(
        text("""SELECT (SELECT count(*) FROM digests WHERE user_id = :u AND read_at IS NULL AND status IN ('sent','preview')),
                       (SELECT count(*) FROM alerts WHERE user_id = :u AND read_at IS NULL)"""),
        {"u": uid},
    ).one()
    return {"digests": int(d), "alerts": int(a), "total": int(d) + int(a)}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
