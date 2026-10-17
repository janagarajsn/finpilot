"""Proactive delivery: digest slot math (timezones, DST, catch-up window), idempotent digests with
the required sections, grounded LLM phrasing + fallback, channels (log sink, Telegram, fallback),
Telegram linking + webhook secret, low-balance alerts with dedupe, ticks, job_runs and the admin
scheduler endpoint."""

from __future__ import annotations

import datetime as dt
import json
import uuid
import zoneinfo

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import system_session, user_session
from finpilot.delivery import log_sink, telegram
from finpilot.digests import alerts as alerts_mod
from finpilot.digests import service as digests
from finpilot.digests.schedule import due_period, last_slot
from finpilot.llm.client import FakeBackend, LLMClient
from finpilot.tasks import digest_tasks
from tests.conftest import needs_pg, needs_redis
from tests.helpers import PASSWORD, StubFX, api_register, bearer, csrf

pytestmark = [needs_pg, needs_redis]
NO_MESH = LLMClient(settings=Settings(mesh_api_key=None))
UTC = dt.UTC


# ---------------------------------------------------------------------------- slot math (pure)
@pytest.mark.parametrize(
    "now, tz, dow, hour, expected",
    [
        (
            dt.datetime(2026, 10, 18, 4, 0, tzinfo=UTC),
            "Asia/Kolkata",
            0,
            9,
            "2026-10-18",
        ),  # Sun 09:30 IST
        (
            dt.datetime(2026, 10, 18, 3, 0, tzinfo=UTC),
            "Asia/Kolkata",
            0,
            9,
            None,
        ),  # 08:30 IST: last week's slot is > 24 h old
        (
            dt.datetime(2026, 10, 19, 3, 0, tzinfo=UTC),
            "Asia/Kolkata",
            0,
            9,
            "2026-10-18",
        ),  # Mon 08:30: catch-up (< 24 h)
        (
            dt.datetime(2026, 10, 19, 4, 0, tzinfo=UTC),
            "Asia/Kolkata",
            0,
            9,
            None,
        ),  # exactly 24 h later: no longer due
        (
            dt.datetime(2026, 10, 23, 22, 0, tzinfo=UTC),
            "America/New_York",
            5,
            18,
            "2026-10-23",
        ),  # Fri 18:00 EDT
        (
            dt.datetime(2026, 11, 1, 14, 30, tzinfo=UTC),
            "America/New_York",
            0,
            9,
            "2026-11-01",
        ),  # DST ends: 09:30 EST
        (
            dt.datetime(2026, 11, 1, 13, 30, tzinfo=UTC),
            "America/New_York",
            0,
            9,
            None,
        ),  # 08:30 EST (not yet)
        (
            dt.datetime(2026, 10, 18, 9, 0, tzinfo=UTC),
            "Not/AZone",
            0,
            9,
            "2026-10-18",
        ),  # bad tz → UTC
    ],
)
def test_due_period(now, tz, dow, hour, expected):
    assert due_period(now, tz, dow, hour) == expected


def test_last_slot_is_local_and_never_in_the_future():
    now = dt.datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # Wednesday
    for dow in range(7):
        s = last_slot(now, "Asia/Kolkata", dow, 23)
        assert s <= now and now - s < dt.timedelta(days=7) and s.hour == 23
        assert (s.weekday() + 1) % 7 == dow


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(log_sink, "OUTBOX", tmp_path)
    return tmp_path


@pytest.fixture
def user(redis_client, outbox):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: NO_MESH
    c = TestClient(app)
    t = api_register(c, "INR")
    h = bearer(t["access_token"])
    assert c.post("/me/sample-data", headers=h).status_code == 201
    uid = uuid.UUID(c.get("/me", headers=h).json()["id"])
    yield c, h, uid
    app.dependency_overrides.clear()


def set_prefs(uid, **kw):
    sets = ", ".join(f"{k} = :{k}" for k in kw)
    with user_session(uid) as s:
        s.execute(text(f"UPDATE user_settings SET {sets} WHERE user_id = :u"), {"u": uid, **kw})


def slot_now(uid) -> tuple[int, int]:
    """A (dow, hour) whose slot started within the last hour for this user."""
    with user_session(uid) as s:
        tz = s.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": uid}).scalar_one()
    local = dt.datetime.now(zoneinfo.ZoneInfo(tz))
    return (local.weekday() + 1) % 7, local.hour


# ---------------------------------------------------------------------------- digests
def test_digest_sections_idempotency_and_inbox(user):
    c, h, uid = user
    out = digests.send_digest(uid, "2026-10-18", llm=NO_MESH, fx=StubFX(), degraded=True)
    again = digests.send_digest(uid, "2026-10-18", llm=NO_MESH, fx=StubFX())
    assert out["status"] == "sent" and out["via"] == "in_app" and again["status"] == "already_sent"
    d = c.get(f"/digests/{out['id']}", headers=h).json()
    body = d["body_text"]
    for section in ("KEY INSIGHT", "ONE ACTION", "NEXT 14 DAYS", "AI service was unavailable"):
        assert section in body, section
    assert (
        d["generated_by"] == "template"
        and d["degraded"]
        and d["subject"].startswith("Your FinPilot week")
    )
    assert "Emergency fund" in body or "GOALS" not in body  # goal line when the sample has a goal
    inbox = c.get("/app/inbox", headers=h)
    assert inbox.status_code == 200 and d["subject"] in inbox.text
    with user_session(uid) as s:
        assert s.execute(text("SELECT count(*) FROM digests")).scalar_one() == 1


def phrase_llm(reply) -> LLMClient:
    return LLMClient(
        settings=Settings(mesh_api_key="x"), backend=FakeBackend({"digest:phrase": reply})
    )


def test_llm_phrasing_is_grounded_or_falls_back(user):
    _, _, uid = user
    good = phrase_llm(
        json.dumps(
            {
                "insight": "Your Amazon spending stood out this week.",
                "action": "Follow the top recommendation to protect your balance.",
            }
        )
    )
    out = digests.send_digest(uid, "2026-10-11", llm=good, fx=StubFX())
    bad = phrase_llm(
        json.dumps({"insight": "You spent ₹9,99,999 on Amazon.", "action": "Save ₹5,55,555 now."})
    )
    out2 = digests.send_digest(uid, "2026-10-04", llm=bad, fx=StubFX())
    with user_session(uid) as s:
        rows = {
            str(r.id): r
            for r in s.execute(
                text("SELECT id, generated_by, body_text, grounding_json FROM digests")
            )
        }
    assert (
        rows[out["id"]].generated_by == "llm"
        and "Amazon spending stood out" in rows[out["id"]].body_text
    )
    fb = rows[out2["id"]]
    assert (
        fb.generated_by == "template"
        and "9,99,999" not in fb.body_text
        and fb.grounding_json["fallback_used"]
    )


def test_email_and_telegram_channels(user, outbox, monkeypatch):
    _, _, uid = user
    set_prefs(uid, digest_channel="email")
    assert digests.send_digest(uid, "2026-09-27", llm=NO_MESH, fx=StubFX())["via"] == "log_sink"
    sent = [json.loads(f.read_text()) for f in outbox.glob("*-email-*.json")]
    assert (
        sent
        and sent[0]["subject"].startswith("Your FinPilot week")
        and "KEY INSIGHT" in sent[0]["body"]
    )
    # Telegram chosen but not linked → kept in-app with a note
    set_prefs(uid, digest_channel="telegram")
    r = digests.send_digest(uid, "2026-09-20", llm=NO_MESH, fx=StubFX())
    with user_session(uid) as s:
        notes = s.execute(
            text("SELECT payload_json FROM digests WHERE id = :i"), {"i": r["id"]}
        ).scalar_one()
    assert r["via"] == "in_app" and "not linked" in notes["delivery_notes"][0]
    # linked + bot token → Bot API
    set_prefs(uid, telegram_chat_id="4242")
    monkeypatch.setattr(telegram, "_token", lambda: "T0K")
    with respx.mock(assert_all_called=True) as m:
        route = m.post("https://api.telegram.org/botT0K/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        r = digests.send_digest(uid, "2026-09-13", llm=NO_MESH, fx=StubFX())
    assert (
        r["via"] == "telegram" and json.loads(route.calls[0].request.content)["chat_id"] == "4242"
    )
    # a failing channel keeps the content and retries next tick
    with respx.mock() as m:
        m.post("https://api.telegram.org/botT0K/sendMessage").mock(return_value=httpx.Response(500))
        fail = digests.send_digest(uid, "2026-09-06", llm=NO_MESH, fx=StubFX())
        assert fail["status"] == "failed"
        m.post("https://api.telegram.org/botT0K/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert digests.send_digest(uid, "2026-09-06", llm=NO_MESH, fx=StubFX())["status"] == "sent"


def test_telegram_link_and_webhook(user, outbox):
    c, h, _ = user
    link = c.post("/settings/telegram/link", headers=h).json()
    assert link["token"] and link["bot_configured"] is False
    upd = {"update_id": 1, "message": {"chat": {"id": 777}, "text": f"/start {link['token']}"}}
    bot = TestClient(app)  # Telegram's servers: no cookies, no CSRF token, no auth
    assert bot.post("/telegram/webhook/wrong", json=upd).status_code == 404
    url = f"/telegram/webhook/{telegram.webhook_secret()}"
    assert bot.post(url, json=upd).json()["result"] == "linked"
    assert c.get("/settings", headers=h).json()["telegram_linked"] is True
    assert bot.post(url, json=upd).json()["result"] == "bad_token"  # single use
    assert (
        bot.post(url, json={"update_id": 2, "message": {"chat": {"id": 777}, "text": "hi"}}).json()[
            "result"
        ]
        == "help"
    )
    replies = [json.loads(f.read_text()) for f in outbox.glob("*-telegram-*.json")]
    assert any("linked" in r["body"] for r in replies)
    assert c.delete("/settings/telegram", headers=h).status_code == 204
    assert c.get("/settings", headers=h).json()["telegram_linked"] is False


def test_low_balance_alert_dedupe_and_banner(user):
    c, h, uid = user
    set_prefs(uid, cash_buffer=100_000_000)  # ₹10 crore cushion: every bad case is below it
    first = alerts_mod.check_user(uid, StubFX())
    assert first["status"] == "raised" and first["via"] == "in_app"
    assert alerts_mod.check_user(uid, StubFX())["status"] == "deduped"
    later = dt.datetime.now(UTC) + dt.timedelta(days=8)
    assert alerts_mod.check_user(uid, StubFX(), now=later)["status"] == "raised"
    listed = c.get("/alerts?unread=true", headers=h).json()
    assert len(listed) == 2 and listed[0]["title"].startswith("Your balance may drop below")
    with system_session("test") as s:  # API-registered users skip the web onboarding step
        s.execute(text("UPDATE users SET onboarded_at = now() WHERE id = :u"), {"u": uid})
    assert "Your balance may drop below" in c.get("/", headers=h).text  # dashboard banner
    for a in listed:
        assert c.post(f"/alerts/{a['id']}/read", headers=h).status_code == 200
    assert c.get("/alerts?unread=true", headers=h).json() == []
    set_prefs(uid, cash_buffer=0)
    set_prefs(uid, risk_alert_enabled=False)
    assert alerts_mod.check_user(uid, StubFX())["status"] == "disabled"


# ---------------------------------------------------------------------------- ticks
def test_digests_due_selects_users_and_pipeline_delivers(user, monkeypatch):
    _, _, uid = user
    dow, hour = slot_now(uid)
    set_prefs(uid, digest_dow=dow, digest_hour=hour)
    assert uid in {u for u, _ in digest_tasks.due_users()}
    set_prefs(uid, digest_enabled=False)
    assert uid not in {u for u, _ in digest_tasks.due_users()}
    set_prefs(uid, digest_enabled=True)
    enqueued = []
    from finpilot.tasks import pipeline_tasks

    monkeypatch.setattr(pipeline_tasks.run_pipeline, "delay", lambda *a: enqueued.append(a))
    digest_tasks.digests_due()
    assert (str(uid), "schedule") in enqueued
    # the scheduled run's deliver node sends the digest (once)
    from finpilot.agents import coordinator

    rid = coordinator.create_run(uid, "schedule")
    out = coordinator.run(uid, rid, trigger="schedule", llm=NO_MESH, fx=StubFX())
    assert out["status"] in ("succeeded", "degraded")
    with user_session(uid) as s:
        d = s.execute(text("SELECT status, run_id FROM digests")).one()
    assert d.status == "sent" and str(d.run_id) == rid
    assert uid not in {u for u, _ in digest_tasks.due_users()}  # sent → not due again


def test_active_users_refresh_and_gc(user, outbox, monkeypatch):
    _, _, uid = user
    assert uid in digest_tasks.active_users()
    calls = []
    from finpilot.tasks import pipeline_tasks

    monkeypatch.setattr(
        pipeline_tasks.run_pipeline, "apply_async", lambda args, countdown=0: calls.append(args)
    )
    assert digest_tasks.refresh_insights()["users"] >= 1 and (str(uid), "schedule") in calls
    with system_session("test") as s:
        s.execute(
            text("""INSERT INTO semantic_cache (user_id, intent, period, question_text, embedding, answer_json,
                          data_version, reporting_currency, as_of_bucket)
                          VALUES (:u, 'free_form', 'this_month', 'q', array_fill(0.1, ARRAY[1536])::vector, '{}', -1, 'INR', 'any')"""),
            {"u": uid},
        )
    old = outbox / "old-email-x.json"
    old.write_text("{}")
    import os

    os.utime(old, (0, 0))
    out = digest_tasks.cache_gc()
    assert out["semantic_cache"] >= 1 and out["outbox_files"] == 1 and not old.exists()


def test_job_runs_and_admin_scheduler(user):
    c, h, uid = user
    from finpilot.tasks.celery_app import heartbeat

    heartbeat.delay()
    with system_session("test") as s:
        st = s.execute(
            text(
                "SELECT status FROM job_runs WHERE task = 'finpilot.tasks.heartbeat' ORDER BY id DESC LIMIT 1"
            )
        ).scalar()
    assert st == "succeeded"
    with system_session("test") as s:  # the first user of a fresh DB is admin: start as a user
        email = s.execute(
            text("UPDATE users SET role = 'user' WHERE id = :u RETURNING email"), {"u": uid}
        ).scalar_one()
    h = bearer(
        c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=csrf(c)).json()[
            "access_token"
        ]
    )
    assert c.get("/scheduler/jobs", headers=h).status_code == 403
    assert c.post("/digest/preview", headers=h).status_code == 403
    with system_session("test") as s:
        email = s.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :u RETURNING email"), {"u": uid}
        ).scalar_one()
    login = c.post(
        "/auth/login", json={"email": email, "password": PASSWORD}, headers=csrf(c)
    ).json()
    ha = bearer(login["access_token"])
    jobs = {j["name"]: j for j in c.get("/scheduler/jobs", headers=ha).json()}
    assert {
        "digests-due",
        "risk-alerts",
        "refresh-insights",
        "goal-snapshots",
        "cache-gc",
        "refresh-fx-rates",
        "memory-consolidation",
    } <= set(jobs)
    assert jobs["heartbeat"]["last_status"] == "succeeded" and jobs["digests-due"]["next_run"]
    pv = c.post("/digest/preview", headers=ha).json()
    assert pv["status"] == "preview"
    assert "[Preview]" in c.get(f"/digests/{pv['id']}", headers=ha).json()["subject"]
    page = c.get("/app/pipeline", headers=ha)
    assert "Scheduler" in page.text and "digests-due" in page.text


def test_figures_from_the_computed_sentences_are_facts(user):
    import re

    _, _, uid = user

    def quote(messages):
        amounts = re.findall(r"₹[\d,]+(?:\.\d+)?", messages[-1]["content"])
        assert amounts, "the insight/action facts carry amounts"
        return json.dumps(
            {"insight": f"This week stood out at {amounts[0]}.", "action": f"Act on {amounts[-1]}."}
        )

    out = digests.send_digest(uid, "2026-08-30", llm=phrase_llm(quote), fx=StubFX())
    assert out["generated_by"] == "llm"
