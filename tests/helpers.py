"""Test helpers: user factories, API clients, transaction inserts."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import text

from finpilot.auth import service as auth
from finpilot.db.session import user_session
from finpilot.llm.client import FakeBackend, _FakeEmbeddings

PASSWORD = "Tr0ub4dor&3-horse-staple"


def unique_email(prefix: str = "u") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def make_user(
    currency: str = "INR", name: str = "Test User", email: str | None = None
) -> auth.TokenPair:
    return auth.register(email or unique_email(), PASSWORD, name, currency, "Asia/Kolkata")


def insert_txn(
    user_id: uuid.UUID,
    *,
    amount: str = "-100.00",
    currency: str = "INR",
    fx_rate: str = "1",
    amount_inr: str | None = None,
    date: dt.date = dt.date(2024, 1, 5),
    source: str = "base",
    description: str = "test txn",
) -> uuid.UUID:
    amount_inr = amount_inr or amount
    with user_session(user_id) as s:
        return s.execute(
            text(
                "INSERT INTO transactions (user_id, date, raw_description, amount, currency, fx_rate, "
                "amount_inr, fx_rate_date, fx_source, hash) VALUES (:u, :d, :desc, :a, :c, :r, :ai, :d, :s, :h) "
                "RETURNING id"
            ),
            {
                "u": user_id,
                "d": date,
                "desc": description,
                "a": Decimal(amount),
                "c": currency,
                "r": Decimal(fx_rate),
                "ai": Decimal(amount_inr),
                "s": source,
                "h": uuid.uuid4().hex,
            },
        ).scalar_one()


def csrf(client: TestClient) -> dict[str, str]:
    """Header echoing the double-submit cookie (fetches a page first if the cookie is missing)."""
    if "fp_csrf" not in client.cookies:
        client.get("/health")
    return {"X-CSRF-Token": client.cookies.get("fp_csrf", "")}


def api_register(
    client: TestClient, currency: str = "INR", email: str | None = None, name: str = "Api User"
) -> dict:
    r = client.post(
        "/auth/register",
        json={
            "email": email or unique_email("api"),
            "password": PASSWORD,
            "name": name,
            "reporting_currency": currency,
            "timezone": "Asia/Kolkata",
        },
        headers=csrf(client),
    )
    assert r.status_code == 201, r.text
    return r.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class StubFX:
    """Deterministic FX for tests: USD = 83.15 INR on/before any date, source 'mcp'."""

    def __init__(self, rate: str = "83.15", source: str = "mcp"):
        self.rate, self.source, self.calls = Decimal(rate), source, 0

    def rate_to_inr(self, currency, on=None):
        from finpilot.fx.service import ResolvedRate

        self.calls += 1
        if currency == "INR":
            return ResolvedRate("INR", Decimal(1), on or dt.date.today(), "base")
        return ResolvedRate(currency, self.rate, on or dt.date.today(), self.source)

    def backfill(self, currency, start, end):
        return 0

    def convert(self, amount, from_currency, to_currency, on=None):
        from finpilot.fx.service import FXService

        return FXService.convert(self, amount, from_currency, to_currency, on)  # type: ignore[arg-type]


class BowBackend(FakeBackend):
    """FakeBackend whose embeddings are hashed bags of words: texts that share words are close
    (cosine), unrelated texts are not. Chat replies come from `responses` as usual."""

    def embed(self, *, model, inputs, **params):
        self.calls.append({"model": model, "inputs": inputs, "kind": "embed"})
        vecs = []
        for t in inputs:
            v = [0.0] * self.embedding_dim
            for w in re.findall(r"[a-z]{3,}", t.lower()):
                w = w[:-1] if w.endswith("s") else w  # crude plural folding
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.embedding_dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / n for x in v])
        return _FakeEmbeddings(vecs)
