"""Reusable IDOR harness. Later phases register cases for their resources:

    CASES.append(IdorCase(
        name="transactions",
        create=lambda api, headers: api.post("/transactions", json=..., headers=headers).json()["id"],
        probes=[("GET", "/transactions/{id}"), ("PATCH", "/transactions/{id}"), ("DELETE", "/transactions/{id}")],
        patch_body={"notes": "pwned"},
    ))

`run_case` creates the resource as user B, then replays every probe as user A and asserts
the server answers 404/403 (never 2xx) and B's resource is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text


@dataclass
class IdorCase:
    name: str
    create: Callable[[TestClient, dict[str, str]], str]
    probes: list[tuple[str, str] | tuple[str, str, dict[str, Any]]]  # (method, path[, body])
    patch_body: dict[str, Any] = field(default_factory=dict)
    read_back: str | None = None  # path for B to verify the resource is untouched


CASES: list[IdorCase] = []


def run_case(
    case: IdorCase, api: TestClient, a_headers: dict[str, str], b_headers: dict[str, str]
) -> None:
    rid = case.create(api, b_headers)
    before = (
        api.get(case.read_back.format(id=rid), headers=b_headers).json() if case.read_back else None
    )
    for probe in case.probes:
        method, path = probe[0], probe[1]
        url = path.format(id=rid)
        kwargs: dict[str, Any] = {"headers": a_headers}
        if method in ("POST", "PATCH", "PUT"):
            body = probe[2] if len(probe) > 2 else case.patch_body  # type: ignore[misc]
            kwargs["json"] = {  # "{id}" inside a body (e.g. a session id) is the resource too
                k: v.format(id=rid) if isinstance(v, str) and "{id}" in v else v
                for k, v in body.items()
            }
        r = api.request(method, url, **kwargs)
        assert r.status_code in (403, 404), (
            f"[{case.name}] {method} {url} as other user → {r.status_code}"
        )
    if case.read_back:
        assert api.get(case.read_back.format(id=rid), headers=b_headers).json() == before


# ---------------------------------------------------------------------------- Phase 2 resources
def _txn(api: TestClient, h: dict[str, str]) -> str:
    r = api.post(
        "/transactions",
        json={
            "kind": "expense",
            "amount": "42.00",
            "date": "2024-05-01",
            "description": "B's private coffee",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _cat(api: TestClient, h: dict[str, str]) -> str:
    r = api.post("/categories", json={"name": "B secret", "flow_type": "expense"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rule(api: TestClient, h: dict[str, str]) -> str:
    r = api.post(
        "/recurring",
        json={
            "kind": "expense",
            "description": "B rent",
            "amount": "1000",
            "frequency": "monthly",
            "start_date": "2030-01-01",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _batch(api: TestClient, h: dict[str, str]) -> str:
    r = api.post(
        "/import/upload",
        files={
            "file": ("b.csv", b"date,description,amount,type\n2024-01-01,x,1,expense\n", "text/csv")
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["batch"]["id"]


CASES += [
    IdorCase(
        "transactions",
        _txn,
        [
            ("GET", "/transactions/{id}"),
            ("PATCH", "/transactions/{id}"),
            ("PATCH", "/transactions/{id}/fx", {"fx_rate": "1"}),
            ("DELETE", "/transactions/{id}"),
        ],
        patch_body={"notes": "pwned"},
        read_back="/transactions/{id}",
    ),
    IdorCase(
        "categories",
        _cat,
        [("PATCH", "/categories/{id}"), ("DELETE", "/categories/{id}")],
        patch_body={"name": "pwned"},
    ),
    IdorCase(
        "recurring",
        _rule,
        [
            ("GET", "/recurring/{id}"),
            ("PATCH", "/recurring/{id}"),
            ("POST", "/recurring/{id}/pause"),
            ("DELETE", "/recurring/{id}"),
        ],
        patch_body={"amount": "1"},
        read_back="/recurring/{id}",
    ),
    IdorCase(
        "imports",
        _batch,
        [
            ("GET", "/import/{id}"),
            ("PATCH", "/import/{id}"),
            ("POST", "/import/{id}/confirm"),
            ("POST", "/import/{id}/undo"),
        ],
        patch_body={"options": {"include_duplicates": True}},
        read_back="/import/{id}",
    ),
]


# ---------------------------------------------------------------------------- Phase 4 resources
def _detected(api: TestClient, h: dict[str, str]) -> str:
    import datetime as dt

    from finpilot.recurring.rules import _add_months

    today = dt.date.today()
    for i in range(1, 5):
        r = api.post(
            "/transactions",
            json={
                "kind": "expense",
                "amount": "649",
                "description": "NETFLIX.COM",
                "date": str(_add_months(today, -i, 5)),
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
    assert api.get("/insights", headers=h).status_code == 200
    rows = api.get("/recurring/detected?all=true", headers=h).json()
    return next(r["id"] for r in rows if r["merchant_norm"] == "NETFLIX")


def _finding(api: TestClient, h: dict[str, str]) -> str:
    import datetime as dt

    for days in (3, 2):
        r = api.post(
            "/transactions",
            json={
                "kind": "expense",
                "amount": "5000",
                "description": "CROMA ELECTRONICS",
                "date": str(dt.date.today() - dt.timedelta(days=days)),
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
    rep = api.get("/insights", headers=h).json()
    return next(f["id"] for f in rep["findings"] if f["type"] == "duplicate_charge")


CASES += [
    IdorCase(
        "detected_recurrences",
        _detected,
        [
            ("POST", "/recurring/detected/{id}/promote"),
            ("POST", "/recurring/detected/{id}/dismiss"),
            ("POST", "/recurring/detected/{id}/restore"),
            ("POST", "/subscriptions/{id}/usage", {"rating": 0}),
        ],
        read_back="/recurring/detected?all=true",
    ),
    IdorCase(
        "analysis_findings",
        _finding,
        [("POST", "/insights/findings/{id}/dismiss"), ("POST", "/insights/findings/{id}/restore")],
        read_back="/insights",
    ),
]


# ---------------------------------------------------------------------------- Phase 5 resources
def _goal(api: TestClient, h: dict[str, str]) -> str:
    r = api.post(
        "/goals",
        json={"kind": "savings_target", "name": "B secret fund", "target_amount": "1000"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


CASES += [
    IdorCase(
        "goals",
        _goal,
        [("GET", "/goals/{id}"), ("PATCH", "/goals/{id}"), ("DELETE", "/goals/{id}")],
        patch_body={"name": "pwned"},
        read_back="/goals/{id}",
    ),
]


# ---------------------------------------------------------------------------- Phase 6 resources
def _recommendation(api: TestClient, h: dict[str, str]) -> str:
    assert api.post("/me/sample-data", headers=h).status_code == 201
    r = api.post("/recommendations/refresh", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["recommendations"][0]["id"]


CASES += [
    IdorCase(
        "recommendations",
        _recommendation,
        [
            ("GET", "/recommendations/{id}/trail"),
            ("POST", "/recommendations/{id}/accept", {}),
            ("POST", "/recommendations/{id}/dismiss", {"reason": "pwned"}),
            ("POST", "/recommendations/{id}/feedback", {"value": "down"}),
        ],
        read_back="/recommendations/{id}/trail",
    ),
]


# ---------------------------------------------------------------------------- Phase 7 resources
def _pipeline_run(api: TestClient, h: dict[str, str]) -> str:
    r = api.post("/pipeline/run", json={}, headers=h)
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def _goal_for_history(api: TestClient, h: dict[str, str]) -> str:
    r = api.post(
        "/goals", json={"kind": "debt_payoff", "name": "B loan", "target_amount": "5000"}, headers=h
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


CASES += [
    IdorCase(
        "pipeline_runs",
        _pipeline_run,
        [
            ("GET", "/pipeline/runs/{id}"),
            ("GET", "/pipeline/runs/{id}/events"),
            ("POST", "/pipeline/runs/{id}/resume"),
        ],
    ),
    IdorCase("goal_snapshots", _goal_for_history, [("GET", "/goals/{id}/snapshots")]),
]


# ---------------------------------------------------------------------------- Phase 9 resources
def _chat(api: TestClient, h: dict[str, str]) -> dict:
    r = api.post("/chat", json={"message": "What are my top categories?"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _memory(api: TestClient, h: dict[str, str]) -> str:
    import uuid as _uuid

    from finpilot.db.session import user_session
    from finpilot.memory.long_term import MemoryIn, PgMemory

    uid = _uuid.UUID(api.get("/me", headers=h).json()["id"])
    with user_session(uid) as s:
        out = PgMemory().write(
            s, uid, MemoryIn(kind="fact", topic="secret", text="B's private plan")
        )
    return str(out.id)


CASES += [
    IdorCase(
        "chat_sessions",
        lambda api, h: _chat(api, h)["session_id"],
        [
            ("GET", "/chat/sessions/{id}"),
            ("DELETE", "/chat/sessions/{id}"),
            ("POST", "/chat", {"message": "continue B's chat", "session_id": "{id}"}),
            ("POST", "/chat/stream", {"message": "continue B's chat", "session_id": "{id}"}),
        ],
        read_back="/chat/sessions/{id}",
    ),
    IdorCase(
        "chat_messages",
        lambda api, h: _chat(api, h)["message_id"],
        [("POST", "/chat/messages/{id}/feedback", {"value": "down"})],
    ),
    IdorCase(
        "memories",
        _memory,
        [("PATCH", "/memory/{id}", {"text": "pwned"}), ("DELETE", "/memory/{id}")],
    ),
]


# ---------------------------------------------------------------------------- Phase 10 resources
def _uid(api: TestClient, h: dict[str, str]) -> Any:
    import uuid as _uuid

    return _uuid.UUID(api.get("/me", headers=h).json()["id"])


def _digest(api: TestClient, h: dict[str, str]) -> str:
    from finpilot.db.session import user_session

    uid = _uid(api, h)
    with user_session(uid) as s:
        return str(
            s.execute(
                text("""INSERT INTO digests (user_id, period_key, channel, status, subject, body_text)
                        VALUES (:u, '2026-10-18', 'in_app', 'sent', 'B week', 'B private digest') RETURNING id"""),
                {"u": uid},
            ).scalar_one()
        )


def _alert(api: TestClient, h: dict[str, str]) -> str:
    from finpilot.db.session import user_session

    uid = _uid(api, h)
    with user_session(uid) as s:
        return str(
            s.execute(
                text("""INSERT INTO alerts (user_id, kind, dedupe_key, severity, title, body, channel)
                        VALUES (:u, 'low_balance', 'low_balance', 'warning', 'B alert', 'B body', 'in_app') RETURNING id"""),
                {"u": uid},
            ).scalar_one()
        )


CASES += [
    IdorCase("digests", _digest, [("GET", "/digests/{id}")]),
    IdorCase("alerts", _alert, [("POST", "/alerts/{id}/read")]),
]
