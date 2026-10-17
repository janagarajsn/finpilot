"""MCP client for runtime MCP servers (official `mcp` SDK, streamable HTTP).

Guarantees per call: allowlist check → Redis cache lookup → circuit breaker → timeout +
retries with backoff → schema-checked dict result → Langfuse span `mcp:<server>.<tool>`.
MCP output is untrusted data: callers validate it with Pydantic before use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import mcp

from finpilot.mcp.registry import MCPServerConfig, get_registry
from finpilot.observability.langfuse import client as langfuse_client
from finpilot.observability.langfuse import langfuse_enabled

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    pass


class ToolNotAllowed(MCPError):
    pass


class CircuitOpen(MCPError):
    pass


class MCPCallFailed(MCPError):
    pass


class KV(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, ex: int | None = None) -> Any: ...


@dataclass
class CircuitBreaker:
    failures_to_open: int
    reset_after_s: float
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        # closed, or half-open once the reset window has elapsed (lets one probe through)
        return self.opened_at is None or time.monotonic() - self.opened_at >= self.reset_after_s

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.allow() else "open"

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failures_to_open:
            self.opened_at = time.monotonic()


_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-sync")


def _run_sync(coro: Any) -> Any:
    """Run a coroutine from sync code, even when called inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _pool.submit(asyncio.run, coro).result()


class MCPClient:
    def __init__(self, cfg: MCPServerConfig, cache: KV | None = None, target: Any = None) -> None:
        """`target` overrides the connection target (URL or in-process server) — tests only."""
        self.cfg = cfg
        self.cache = cache
        self._target: Any = target if target is not None else cfg.url
        self.breaker = CircuitBreaker(cfg.breaker_failures, cfg.breaker_reset_s)
        self._lock = threading.Lock()

    def _cache_key(self, tool: str, args: dict[str, Any]) -> str:
        blob = json.dumps(args, sort_keys=True, default=str)
        return f"mcp:{self.cfg.name}:{tool}:{hashlib.sha256(blob.encode()).hexdigest()[:24]}"

    async def _call_once(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with mcp.Client(self._target, read_timeout_seconds=self.cfg.timeout_s) as c:
            res = await asyncio.wait_for(c.call_tool(tool, args), timeout=self.cfg.timeout_s)
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            err_text = " ".join(getattr(b, "text", "") for b in (res.content or []))
            raise MCPCallFailed(f"{self.cfg.name}.{tool} returned error: {err_text[:200]}")
        structured = getattr(res, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        for block in res.content or []:
            text = getattr(block, "text", None)
            if text:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
        raise MCPCallFailed(f"{self.cfg.name}.{tool}: no structured result")

    async def acall(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        if tool not in self.cfg.allowed_tools:
            raise ToolNotAllowed(f"{self.cfg.name}.{tool} is not allowlisted")
        if not self.cfg.enabled:
            raise MCPError(f"MCP server {self.cfg.name} disabled")

        key = self._cache_key(tool, args)
        if self.cache is not None and self.cfg.cache_ttl_s:
            try:
                hit = self.cache.get(key)
                if hit:
                    return json.loads(hit)
            except Exception:
                log.debug("mcp cache get failed", exc_info=True)

        with self._lock:
            if not self.breaker.allow():
                raise CircuitOpen(f"circuit open for MCP server {self.cfg.name}")

        span_cm = (
            langfuse_client().start_as_current_observation(
                name=f"mcp:{self.cfg.name}.{tool}", as_type="tool", input=args
            )
            if langfuse_enabled()
            else None
        )
        span = span_cm.__enter__() if span_cm else None
        last: Exception | None = None
        try:
            for attempt in range(self.cfg.retries + 1):
                try:
                    result = await self._call_once(tool, args)
                    with self._lock:
                        self.breaker.record_success()
                    if span:
                        span.update(output=result, metadata={"attempts": str(attempt + 1)})
                    if self.cache is not None and self.cfg.cache_ttl_s:
                        try:
                            self.cache.set(key, json.dumps(result), ex=self.cfg.cache_ttl_s)
                        except Exception:
                            log.debug("mcp cache set failed", exc_info=True)
                    return result
                except MCPCallFailed:
                    raise  # tool-level error: retrying will not help
                except Exception as e:  # transport / timeout
                    last = e
                    if attempt < self.cfg.retries:
                        await asyncio.sleep(min(0.2 * 2**attempt, 2.0))
            with self._lock:
                self.breaker.record_failure()
            if span:
                span.update(level="ERROR", status_message=type(last).__name__)
            raise MCPError(
                f"{self.cfg.name}.{tool} failed: {type(last).__name__}: {last}"
            ) from last
        finally:
            if span_cm:
                span_cm.__exit__(None, None, None)

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return _run_sync(self.acall(tool, args))


_clients: dict[str, MCPClient] = {}
_clients_lock = threading.Lock()


def get_mcp_client(name: str, cache: KV | None = None) -> MCPClient:
    with _clients_lock:
        if name not in _clients:
            cfg = get_registry().get(name)
            if cfg is None:
                raise MCPError(f"unknown MCP server {name}")
            _clients[name] = MCPClient(cfg, cache=cache)
        return _clients[name]
