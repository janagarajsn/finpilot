"""MCP client ↔ FX MCP server contract tests (in-process and over real streamable HTTP)."""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import threading
import time
from decimal import Decimal

import httpx
import pytest

from finpilot.fx.provider import FXSourceError, Rate
from finpilot.mcp.client import CircuitOpen, MCPCallFailed, MCPClient, MCPError, ToolNotAllowed
from finpilot.mcp.registry import MCPServerConfig, load_registry
from mcp_servers.fx.server import build_server

ALLOWED = ["get_rate", "get_rates_range", "convert", "list_supported_currencies"]


class StubProvider:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def get_rate(self, base, quote, date=None):
        self.calls += 1
        if self.fail:
            raise FXSourceError("down")
        return Rate(date or dt.date(2024, 1, 5), base.upper(), quote.upper(), Decimal("83.15"))

    def get_rates_range(self, base, quotes, start, end):
        return [Rate(start, base, q, Decimal("83.10")) for q in quotes]


class DictKV:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, ex=None):
        self.d[k] = v


def cfg(**kw) -> MCPServerConfig:
    base = dict(
        name="fx",
        url="http://unused/mcp",
        timeout_s=3,
        retries=1,
        breaker_failures=2,
        breaker_reset_s=0.3,
        cache_ttl_s=60,
        allowed_tools=ALLOWED,
    )
    return MCPServerConfig(**{**base, **kw})


@pytest.fixture
def stub():
    return StubProvider()


@pytest.fixture
def inproc(stub):
    server = build_server(stub)
    return MCPClient(cfg(), cache=DictKV(), target=server._mcp_server)


def test_server_exposes_exactly_the_allowlisted_tools(stub):
    import mcp

    async def go():
        async with mcp.Client(build_server(stub)._mcp_server) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    assert asyncio.run(go()) == sorted(ALLOWED)


def test_convert_contract(inproc):
    out = inproc.call(
        "convert",
        {"amount": "12.00", "from_currency": "USD", "to_currency": "INR", "date": "2024-01-07"},
    )
    assert out == {
        "amount": "12.00",
        "converted": "997.80",
        "date": "2024-01-07",
        "base": "USD",
        "quote": "INR",
        "rate": "83.15",
    }


def test_rates_range_and_currencies(inproc):
    out = inproc.call(
        "get_rates_range",
        {"base": "USD", "quotes": ["INR"], "start": "2024-01-01", "end": "2024-01-03"},
    )
    assert out["rates"][0]["rate"] == "83.10"
    assert inproc.call("list_supported_currencies")["currencies"] == ["INR", "USD"]


def test_unsupported_currency_is_tool_error_not_retried(inproc, stub):
    with pytest.raises(MCPCallFailed, match="unsupported currency"):
        inproc.call("get_rate", {"base": "EUR", "quote": "INR"})
    assert inproc.breaker.state == "closed"


def test_allowlist_is_enforced_before_any_io(stub):
    c = MCPClient(cfg(allowed_tools=["get_rate"]), target=build_server(stub)._mcp_server)
    with pytest.raises(ToolNotAllowed):
        c.call("convert", {"amount": "1", "from_currency": "USD", "to_currency": "INR"})
    assert stub.calls == 0


def test_results_are_cached(inproc, stub):
    args = {"base": "USD", "quote": "INR", "date": "2024-01-05"}
    a = inproc.call("get_rate", args)
    b = inproc.call("get_rate", args)
    assert a == b and stub.calls == 1


def test_circuit_breaker_opens_and_recovers():
    c = MCPClient(cfg(retries=0, timeout_s=0.5, cache_ttl_s=0), target="http://127.0.0.1:9/mcp")
    for _ in range(2):
        with pytest.raises(MCPError):
            c.call("get_rate", {"base": "USD", "quote": "INR"})
    assert c.breaker.state == "open"
    with pytest.raises(CircuitOpen):
        c.call("get_rate", {"base": "USD", "quote": "INR"})
    time.sleep(0.35)
    assert c.breaker.state == "half_open"
    c._target = build_server(StubProvider())._mcp_server  # server is back
    assert c.call("get_rate", {"base": "USD", "quote": "INR"})["rate"] == "83.15"
    assert c.breaker.state == "closed"


def test_sync_call_works_inside_running_event_loop(inproc):
    async def go():
        return inproc.call("get_rate", {"base": "USD", "quote": "INR"})

    assert asyncio.run(go())["quote"] == "INR"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def http_server():
    port = _free_port()
    server = build_server(StubProvider())
    t = threading.Thread(
        target=lambda: server.run(transport="http", host="127.0.0.1", port=port, show_banner=False),
        daemon=True,
    )
    t.start()
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    return port


def test_streamable_http_end_to_end(http_server):
    assert httpx.get(f"http://127.0.0.1:{http_server}/health").json()["status"] == "ok"
    c = MCPClient(cfg(url=f"http://127.0.0.1:{http_server}/mcp"))
    out = c.call("convert", {"amount": "1", "from_currency": "USD", "to_currency": "INR"})
    assert out["converted"] == "83.15"


def test_registry_loads_config_with_env_expansion(monkeypatch):
    monkeypatch.setenv("FX_MCP_URL", "http://fx-mcp:8765/mcp")
    reg = load_registry()
    assert reg["fx"].url == "http://fx-mcp:8765/mcp"
    assert set(reg["fx"].allowed_tools) == set(ALLOWED)
