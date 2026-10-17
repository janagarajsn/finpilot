from __future__ import annotations

import json

import httpx
import pytest

from finpilot.config import Settings
from finpilot.llm.client import (
    CallRecord,
    FakeBackend,
    LLMClient,
    LLMError,
    LLMUnavailable,
    RateLimitError,
    estimate_cost,
)
from finpilot.llm.prompt_registry import get_registry
from finpilot.llm.schemas import HelloResponse

VARS = {"app_name": "FinPilot", "user_text": "hi"}


class DictCache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, ttl):
        self.d[k] = v


def make(backend=None, **kw) -> tuple[LLMClient, list[CallRecord]]:
    c = LLMClient(settings=Settings(mesh_max_retries=3, **kw), backend=backend or FakeBackend())
    recs: list[CallRecord] = []
    c.add_sink(recs.append)
    return c, recs


def test_unconfigured_client_raises_unavailable():
    c = LLMClient(settings=Settings())
    assert not c.available
    with pytest.raises(LLMUnavailable):
        c.chat("system/hello", VARS, agent="t")
    assert c.embeddings_available() is False


def test_chat_json_validates_and_records_call():
    c, recs = make(FakeBackend({"probe:hello": {"ok": True, "echo": "hi"}}))
    parsed, _res = c.chat_json("system/hello", VARS, HelloResponse, agent="probe", user_id="u1")
    assert parsed == HelloResponse(ok=True, echo="hi")
    assert len(recs) == 1 and recs[0].agent == "probe" and recs[0].prompt_name == "hello"
    latest = get_registry().get("system/hello").meta.version
    assert recs[0].prompt_version == latest and recs[0].kind == "json" and recs[0].user_id == "u1"
    sent = c.backend.calls[0]
    assert sent["messages"][0]["role"] == "system"  # STATIC first
    assert sent["response_format"] == {"type": "json_object"} and sent["temperature"] == 0


def test_chat_json_repairs_once_then_fails():
    replies = iter(["not json", json.dumps({"ok": True, "echo": "x"})])
    c, _ = make(FakeBackend({"*": lambda m: next(replies)}))
    parsed, _ = c.chat_json("system/hello", VARS, HelloResponse, agent="t")
    assert parsed.echo == "x"
    c2, _ = make(FakeBackend({"*": "still not json"}))
    with pytest.raises(LLMError):
        c2.chat_json("system/hello", VARS, HelloResponse, agent="t")


def test_json_in_code_fences_is_accepted():
    c, _ = make(FakeBackend({"*": '```json\n{"ok": true, "echo": "f"}\n```'}))
    assert c.chat_json("system/hello", VARS, HelloResponse, agent="t")[0].echo == "f"


def test_l1_cache_hit_skips_backend():
    c, recs = make(FakeBackend({"*": {"ok": True, "echo": "c"}}))
    c.cache = DictCache()
    c.chat("system/hello", VARS, agent="t")
    r2 = c.chat("system/hello", VARS, agent="t")
    assert r2.cache_layer == "L1"
    assert len(c.backend.calls) == 1
    assert [r.cache_layer for r in recs] == [None, "L1"]


def test_retries_transient_errors_then_succeeds():
    attempts = {"n": 0}

    def flaky(_msgs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimitError(
                "slow down",
                response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                body=None,
            )
        return {"ok": True, "echo": "r"}

    c, recs = make(FakeBackend({"*": flaky}))
    assert c.chat_json("system/hello", VARS, HelloResponse, agent="t")[0].echo == "r"
    assert attempts["n"] == 3 and recs[-1].ok


def test_exhausted_retries_raise_unavailable_and_record_failure():
    def boom(_m):
        raise RateLimitError(
            "no", response=httpx.Response(429, request=httpx.Request("POST", "http://x")), body=None
        )

    c, recs = make(FakeBackend({"*": boom}))
    with pytest.raises(LLMUnavailable):
        c.chat("system/hello", VARS, agent="t")
    assert recs[-1].ok is False and recs[-1].error == "RateLimitError"


def test_tool_calls_are_parsed():
    tc = {
        "tool_calls": [
            {"id": "c1", "function": {"name": "get_forecast", "arguments": '{"days": 30}'}}
        ]
    }
    c, _ = make(FakeBackend({"*": tc}))
    res = c.chat_tools(
        "system/hello",
        VARS,
        tools=[{"type": "function", "function": {"name": "get_forecast"}}],
        agent="chat",
    )
    assert res.tool_calls == [{"id": "c1", "name": "get_forecast", "arguments": '{"days": 30}'}]


def test_embeddings_probe_and_fallback():
    ok, _recs = make(FakeBackend(embedding_dim=8))
    assert ok.embeddings_available()
    v = ok.embed(["a", "a", "b"], agent="mem")
    assert len(v) == 3 and len(v[0]) == 8 and v[0] == v[1] != v[2]
    bad, _ = make(FakeBackend(fail_embeddings=True))
    assert bad.embeddings_available() is False
    with pytest.raises(LLMUnavailable):
        bad.embed(["x"], agent="mem")
    forced_off, _ = make(FakeBackend(), embeddings_enabled=False)
    assert forced_off.embeddings_available() is False


def test_tier_selects_model():
    c, _ = make(FakeBackend({"*": "x"}), mesh_model_fast="f/fast", mesh_model_smart="s/smart")
    c.chat("system/hello", VARS, agent="t")
    c.chat("system/hello", VARS, agent="t", tier="smart")
    assert [x["model"] for x in c.backend.calls] == ["f/fast", "s/smart"]


def test_cost_estimate_uses_pricing_table():
    assert estimate_cost("openai/gpt-4o-mini", 1_000_000, 0, 0) == pytest.approx(0.15)
    assert estimate_cost("openai/gpt-4o-mini", 1_000_000, 1_000_000, 0) == pytest.approx(0.075)
    assert estimate_cost("unknown/model", 10, 0, 10) == 0.0
