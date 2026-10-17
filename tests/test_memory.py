"""Long- and short-term memory: write/dedupe/supersede/expiry/blocked content, retrieval scoring
(vector and lexical), deletion removes recall, the writer (chat consolidation + decisions), the
rolling summary, and the Memory API."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.api.main import app
from finpilot.api.routes_forecast import llm_client
from finpilot.api.routes_fx import fx_service
from finpilot.config import Settings
from finpilot.db.session import user_session
from finpilot.llm.client import LLMClient
from finpilot.memory import short_term, writer
from finpilot.memory.long_term import MemoryIn, PgMemory
from finpilot.memory.retrieval import recall
from finpilot.redis_client import get_redis
from finpilot.tasks.memory_tasks import memory_consolidation
from tests.conftest import needs_pg, needs_redis
from tests.helpers import BowBackend, StubFX, api_register, bearer, make_user

pytestmark = [needs_pg, needs_redis]
NO_MESH = LLMClient(settings=Settings(mesh_api_key=None))


def bow_llm(responses: dict | None = None) -> LLMClient:
    return LLMClient(
        settings=Settings(mesh_api_key="x", embeddings_enabled=True), backend=BowBackend(responses)
    )


@pytest.fixture
def uid(redis_client) -> uuid.UUID:
    return make_user().user_id


def write(uid, llm=None, **kw) -> object:
    with user_session(uid) as s:
        return PgMemory(llm).write(s, uid, MemoryIn(**kw))


def active(uid) -> list[dict]:
    with user_session(uid) as s:
        return PgMemory().list_all(s, uid)


@pytest.mark.parametrize("llm", [None, "bow"], ids=["lexical", "vector"])
def test_dedupe_supersede_expiry_and_blocked(uid, llm):
    llm = bow_llm() if llm else None
    a = write(
        uid,
        llm,
        kind="goal_context",
        topic="japan_trip",
        text="Saving for a Japan trip in March 2027",
    )
    same = write(
        uid,
        llm,
        kind="goal_context",
        topic="japan_trip",
        text="saving for a Japan trip in March 2027.",
    )
    assert (a.action, same.action, same.id) == ("added", "merged", a.id)
    new = write(
        uid, llm, kind="goal_context", topic="Japan trip", text="The Japan trip moved to May 2027"
    )
    assert new.action == "superseded" and new.previous_id == a.id
    assert [m["text"] for m in active(uid)] == ["The Japan trip moved to May 2027"]
    # expired facts are not active; blocked content is never stored
    write(
        uid,
        llm,
        kind="fact",
        topic="bonus",
        text="Expecting a bonus this month",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
    )
    assert (
        write(uid, llm, kind="fact", topic="acct", text="My account number is 123456789012").action
        == "blocked"
    )
    assert (
        write(uid, llm, kind="fact", topic="pw", text="my password is hunter2").action == "blocked"
    )
    assert len(active(uid)) == 1
    with user_session(uid) as s:
        assert len(PgMemory().list_all(s, uid, include_inactive=True)) == 3


def test_vector_retrieval_ranks_relevant_and_pinned_first(uid):
    llm = bow_llm()
    write(
        uid,
        llm,
        kind="preference",
        topic="netflix",
        text="Netflix is shared with family, do not suggest cancelling it",
        importance=0.8,
    )
    write(
        uid, llm, kind="fact", topic="income", text="Income arrives irregularly from freelance work"
    )
    write(
        uid, llm, kind="preference", topic="digest_day", text="Prefers the weekly digest on Sunday"
    )
    pin = write(uid, llm, kind="fact", topic="city", text="Lives in Pune")
    with user_session(uid) as s:
        PgMemory(llm).update(s, uid, pin.id, pinned=True)
        hits = recall(s, uid, "is netflix shared with family?", llm)
    assert hits[0]["text"] == "Lives in Pune" and hits[0]["via"] == "pinned"
    assert hits[1]["text"].startswith("Netflix is shared")
    assert "Income arrives irregularly from freelance work" not in [h["text"] for h in hits]


def test_lexical_retrieval_without_embeddings(uid):
    write(uid, None, kind="preference", topic="netflix", text="Netflix is shared with family")
    write(uid, None, kind="fact", topic="income", text="Income arrives irregularly")
    with user_session(uid) as s:
        hits = recall(s, uid, "Is my Netflix subscription worth keeping?", None)
    assert [h["text"] for h in hits] == ["Netflix is shared with family"] and hits[0][
        "via"
    ] == "lexical"


def test_delete_removes_recall_including_superseded_history(uid):
    llm = bow_llm()
    write(uid, llm, kind="preference", topic="digest_day", text="Prefers the digest on Sunday")
    new = write(
        uid, llm, kind="preference", topic="digest_day", text="Prefers the digest on Monday"
    )
    with user_session(uid) as s:
        assert PgMemory().delete(s, uid, new.id)
        assert recall(s, uid, "digest day", llm) == []  # the Sunday version did not come back
        assert s.execute(text("SELECT count(*) FROM memories")).scalar_one() == 0
    write(uid, llm, kind="fact", topic="x", text="Has two cats")
    with user_session(uid) as s:
        assert PgMemory().wipe(s, uid) == 1


def test_memories_never_cross_users(redis_client):
    a, b = make_user().user_id, make_user().user_id
    write(a, None, kind="fact", topic="secret", text="Planning a surprise party for Priya")
    with user_session(b) as s:
        assert recall(s, b, "surprise party Priya", None) == []
        assert s.execute(text("SELECT count(*) FROM memories")).scalar_one() == 0


def _session_with_turns(uid, turns: list[tuple[str, str]], idle_minutes: int = 30) -> uuid.UUID:
    with user_session(uid) as s:
        sid = s.execute(
            text("INSERT INTO chat_sessions (user_id, title) VALUES (:u, 't') RETURNING id"),
            {"u": uid},
        ).scalar_one()
        for role, content in turns:
            s.execute(
                text(
                    "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (:u, :s, :r, :c)"
                ),
                {"u": uid, "s": sid, "r": role, "c": content},
            )
        s.execute(
            text(
                "UPDATE chat_sessions SET last_message_at = now() - make_interval(mins => :m) WHERE id = :s"
            ),
            {"m": idle_minutes, "s": sid},
        )
    return sid


def extraction(messages):
    content = messages[-1]["content"]
    assert "<data>" in content and "Netflix" in content
    return json.dumps(
        {
            "memories": [
                {
                    "kind": "preference",
                    "topic": "netflix",
                    "text": "Netflix is shared with family; don't suggest cancelling it",
                    "importance": 0.8,
                    "confidence": 0.9,
                },
                {
                    "kind": "goal_context",
                    "topic": "japan_trip",
                    "text": "Saving for a Japan trip",
                    "importance": 0.7,
                    "confidence": 0.9,
                    "expires_on": "2099-03-31",
                },
                {"kind": "fact", "topic": "vague", "text": "Might like cricket", "confidence": 0.3},
                {
                    "kind": "fact",
                    "topic": "card",
                    "text": "Card 4111 1111 1111 1111 is the main one",
                    "confidence": 0.9,
                },
                {
                    "kind": "episode",
                    "topic": "old",
                    "text": "Went to Goa",
                    "confidence": 0.9,
                    "expires_on": "2001-01-01",
                },
            ]
        }
    )


def test_writer_consolidates_idle_sessions_once(uid, monkeypatch):
    llm = bow_llm({"memory_writer:extract": extraction})
    sid = _session_with_turns(
        uid,
        [
            ("user", "Netflix is shared with my family, never suggest cancelling it."),
            ("assistant", "Got it."),
            ("user", "I'm saving for Japan next March."),
        ],
    )
    fresh = _session_with_turns(uid, [("user", "hello Netflix")], idle_minutes=1)  # not idle yet
    from finpilot.tasks import memory_tasks

    monkeypatch.setattr(memory_tasks, "_llm", lambda: llm)
    out = memory_consolidation()
    assert out["sessions"] >= 1
    texts = sorted(m["text"] for m in active(uid))
    assert texts == [
        "Netflix is shared with family; don't suggest cancelling it",
        "Saving for a Japan trip",
    ]
    with user_session(uid) as s:  # low confidence, card number and past events were dropped
        trip = next(m for m in PgMemory().list_all(s, uid) if m["topic"] == "japan_trip")
        assert (
            trip["expires_at"].date() == dt.date(2099, 4, 1) and trip["source_ref"] == f"chat:{sid}"
        )
        assert writer.consolidate_session(s, uid, sid, llm, dt.date.today()) == []  # nothing new
        pending = {r["id"] for r in writer.idle_sessions(s)}
    assert sid not in pending and fresh not in pending


def test_writer_without_mesh_leaves_session_pending(uid):
    sid = _session_with_turns(uid, [("user", "I get paid on the 7th")])
    with user_session(uid) as s:
        assert writer.consolidate_session(s, uid, sid, NO_MESH, dt.date.today()) is None
        assert sid in {r["id"] for r in writer.idle_sessions(s)}


def test_rolling_summary_folds_old_turns(uid):
    calls = []

    def summarize(messages):
        calls.append(messages[-1]["content"])
        return json.dumps({"summary": "- talked about groceries"})

    llm = bow_llm({"chat:summarize": summarize})
    with user_session(uid) as s:
        sid = s.execute(
            text("INSERT INTO chat_sessions (user_id) VALUES (:u) RETURNING id"), {"u": uid}
        ).scalar_one()
    for i in range(8):  # 16 turns > 12; stored first, as the chat agent does
        turns = [
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": f"answer {i}"},
        ]
        with user_session(uid) as s:
            for t in turns:
                s.execute(
                    text(
                        "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (:u, :s, :r, :c)"
                    ),
                    {"u": uid, "s": sid, "r": t["role"], "c": t["content"]},
                )
            short_term.append(s, uid, sid, turns, llm)
    with user_session(uid) as s:
        w = short_term.window(s, uid, sid)
    assert len(w["turns"]) == 12 and w["turns"][0]["content"] == "question 2"
    assert w["summary"] == "- talked about groceries" and "question 0" in calls[0]
    # Redis lost: the window is rebuilt from chat_messages (the newest 12)
    get_redis().delete(short_term.key(uid, sid))
    with user_session(uid) as s:
        rebuilt = short_term.window(s, uid, sid)["turns"]
    assert [t["content"] for t in rebuilt] == [t["content"] for t in w["turns"]]


def test_without_mesh_the_summary_keeps_a_truncated_transcript(uid):
    with user_session(uid) as s:
        sid = s.execute(
            text("INSERT INTO chat_sessions (user_id) VALUES (:u) RETURNING id"), {"u": uid}
        ).scalar_one()
        short_term.fold(s, uid, sid, [{"role": "user", "content": "what about rent?"}], None)
        assert "what about rent?" in short_term.summary(s, uid, sid)


@pytest.fixture
def api(redis_client):
    app.dependency_overrides[fx_service] = lambda: StubFX()
    app.dependency_overrides[llm_client] = lambda: NO_MESH
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_memory_api_and_decision_memories(api):
    t = api_register(api)
    h = bearer(t["access_token"])
    uid = uuid.UUID(api.get("/me", headers=h).json()["id"])
    write(uid, None, kind="fact", topic="city", text="Lives in Pune")
    m = api.get("/memory", headers=h).json()
    assert [x["text"] for x in m] == ["Lives in Pune"]
    mid = m[0]["id"]
    r = api.patch(
        f"/memory/{mid}", json={"text": "Lives in Bengaluru", "pinned": True}, headers=h
    ).json()
    assert r["text"] == "Lives in Bengaluru" and r["pinned"] and r["source"] == "user"
    assert (
        api.patch(f"/memory/{mid}", json={"text": "my PIN is 1234"}, headers=h).status_code == 422
    )
    assert (
        api.patch(f"/memory/{uuid.uuid4()}", json={"pinned": False}, headers=h).status_code == 404
    )
    # accepting / dismissing advice is remembered (decision slot per candidate)
    assert api.post("/me/sample-data", headers=h).status_code == 201
    recs = api.get("/recommendations", headers=h).json()["recommendations"]
    assert recs, "sample data should produce template recommendations"
    if recs:
        rid = recs[0]["id"]
        api.post(
            f"/recommendations/{rid}/dismiss", json={"reason": "we share it with family"}, headers=h
        )
        dec = [x for x in api.get("/memory", headers=h).json() if x["kind"] == "decision"]
        assert (
            len(dec) == 1
            and "we share it with family" in dec[0]["text"]
            and recs[0]["title"] in dec[0]["text"]
        )
        api.post(f"/recommendations/{rid}/accept", headers=h)
        dec = [x for x in api.get("/memory", headers=h).json() if x["kind"] == "decision"]
        assert len(dec) == 1 and dec[0]["text"].startswith("Accepted")  # superseded, not added
    assert api.delete(f"/memory/{mid}", headers=h).status_code == 204
    assert api.delete(f"/memory/{mid}", headers=h).status_code == 404
    assert api.delete("/memory", headers=h).json()["deleted"] >= 0
    assert api.get("/memory", headers=h).json() == []
