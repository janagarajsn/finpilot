"""The ONLY module that talks to an LLM / embedding model. All traffic goes to Mesh API.

Everything else in the codebase calls `get_llm()` and uses:
  - `chat(...)`        free text completion from a versioned prompt
  - `chat_json(...)`   JSON-mode completion validated into a Pydantic model
  - `chat_tools(...)`  one tool-calling turn (the agent loop lives in the caller)
  - `embed(...)`       embeddings (only if `embeddings_available()`)

The client is the Langfuse drop-in OpenAI wrapper, so every call is traced. Each call is
also reported to registered `CallSink`s (the DB layer writes `llm_calls` rows) and can be
served from / stored to an `LLMCache` (L1 exact cache; plugged in by the caching layer).
Tests swap the backend with `use_backend(FakeBackend(...))` — no network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, TypeVar

import yaml
from langfuse.openai import OpenAI  # Langfuse-traced OpenAI-compatible client (Mesh-only)
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from finpilot.config import Settings, get_settings
from finpilot.llm.prompt_registry import Prompt, get_registry
from finpilot.observability.langfuse import init_langfuse, score_current_trace, trace_context

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

MESH_BASE_URL = "https://api.meshapi.ai/v1"
_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Mesh not configured or unreachable → callers switch to degraded mode."""


# --------------------------------------------------------------------------- records/hooks
@dataclass
class CallRecord:
    agent: str
    model: str
    kind: str  # chat | json | tools | embed
    prompt_name: str | None
    prompt_version: int | None
    user_id: str | None
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0  # actually spent on this call
    cache_layer: str | None = None  # None (miss) | "L1"
    saved_cost_usd: float = 0.0  # cost avoided by an L1 cache hit (original call's usage)
    ok: bool = True
    error: str | None = None


CallSink = Callable[[CallRecord], None]


class LLMCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def set(self, key: str, value: dict[str, Any], ttl: int) -> None: ...


@dataclass
class LLMResult:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cache_layer: str | None = None
    raw_message: dict[str, Any] | None = None


# --------------------------------------------------------------------------- backends
class Backend(Protocol):
    def chat(self, *, model: str, messages: list[dict[str, Any]], **params: Any) -> Any: ...
    def embed(self, *, model: str, inputs: list[str], **params: Any) -> Any: ...


class MeshBackend:
    """Real backend: Langfuse-wrapped OpenAI client pointed at Mesh."""

    def __init__(self, settings: Settings) -> None:
        if not settings.mesh_configured:
            raise LLMUnavailable("MESH_API_KEY is not set")
        init_langfuse(settings)
        assert settings.mesh_api_key is not None
        self._client = OpenAI(
            base_url=MESH_BASE_URL,
            api_key=settings.mesh_api_key.get_secret_value(),
            timeout=settings.mesh_timeout_s,
            max_retries=0,  # retries are ours (tenacity) so they are visible and bounded
        )

    def chat(self, *, model: str, messages: list[dict[str, Any]], **params: Any) -> Any:
        return self._client.chat.completions.create(model=model, messages=messages, **params)  # type: ignore[arg-type]

    def embed(self, *, model: str, inputs: list[str], **params: Any) -> Any:
        return self._client.embeddings.create(model=model, input=inputs, **params)


class FakeBackend:
    """Recorded-response fake for tests. `responses` maps prompt name (or '*') to a reply.

    A reply may be a str (assistant content), a dict (serialized to JSON content, or an
    OpenAI-style message dict if it has a 'tool_calls' key), or a callable(messages)->reply.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        embedding_dim: int = 1536,
        fail_embeddings: bool = False,
    ) -> None:
        self.responses = responses or {}
        self.embedding_dim = embedding_dim
        self.fail_embeddings = fail_embeddings
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, model: str, messages: list[dict[str, Any]], **params: Any) -> Any:
        self.calls.append({"model": model, "messages": messages, **params})
        name = params.get("name", "")
        reply = self.responses.get(name, self.responses.get("*", ""))
        if callable(reply):
            reply = reply(messages)
        msg: dict[str, Any]
        if isinstance(reply, dict) and "tool_calls" in reply:
            msg = {
                "role": "assistant",
                "content": reply.get("content"),
                "tool_calls": reply["tool_calls"],
            }
        else:
            content = json.dumps(reply) if isinstance(reply, dict | list) else str(reply)
            msg = {"role": "assistant", "content": content}
        return _FakeCompletion(
            model, msg, sum(len(str(m.get("content", ""))) for m in messages) // 4
        )

    def embed(self, *, model: str, inputs: list[str], **params: Any) -> Any:
        if self.fail_embeddings:
            raise BadRequestError(
                "embeddings unsupported", response=_fake_http_response(400), body=None
            )
        self.calls.append({"model": model, "inputs": inputs, "kind": "embed"})
        return _FakeEmbeddings([_hash_vector(t, self.embedding_dim) for t in inputs])


def _hash_vector(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-embedding: identical text → identical unit vector."""
    out: list[float] = []
    i = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{i}:{text.lower().strip()}".encode()).digest()
        out.extend((b - 127.5) / 127.5 for b in h)
        i += 1
    v = out[:dim]
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def _fake_http_response(status: int) -> Any:
    import httpx

    return httpx.Response(status, request=httpx.Request("POST", MESH_BASE_URL))


class _Obj(dict):
    __getattr__ = dict.get  # type: ignore[assignment]


def _FakeCompletion(model: str, message: dict[str, Any], prompt_tokens: int) -> Any:
    usage = _Obj(
        prompt_tokens=prompt_tokens,
        completion_tokens=len(str(message.get("content") or "")) // 4,
        prompt_tokens_details=_Obj(cached_tokens=0),
    )
    tcs = [
        _Obj(id=t["id"], type="function", function=_Obj(**t["function"]))
        for t in message.get("tool_calls") or []
    ]
    return _Obj(
        model=model,
        choices=[
            _Obj(
                message=_Obj(
                    role="assistant", content=message.get("content"), tool_calls=tcs or None
                )
            )
        ],
        usage=usage,
    )


def _FakeEmbeddings(vectors: list[list[float]]) -> Any:
    return _Obj(
        data=[_Obj(embedding=v, index=i) for i, v in enumerate(vectors)],
        usage=_Obj(prompt_tokens=0),
    )


# --------------------------------------------------------------------------- pricing
@lru_cache
def _pricing() -> dict[str, dict[str, float]]:
    path = get_settings().config_dir / "model_pricing.yml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def estimate_cost(
    model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int
) -> float:
    p = _pricing().get(model)
    if not p:
        return 0.0
    uncached = max(prompt_tokens - cached_tokens, 0)
    return round(
        (
            uncached * p["input"]
            + cached_tokens * p["cached_input"]
            + completion_tokens * p["output"]
        )
        / 1e6,
        8,
    )


# --------------------------------------------------------------------------- client
class LLMClient:
    def __init__(self, settings: Settings | None = None, backend: Backend | None = None) -> None:
        self.settings = settings or get_settings()
        self._backend = backend
        self._sinks: list[CallSink] = []
        self.cache: LLMCache | None = None
        self._embed_ok: bool | None = self.settings.embeddings_enabled

    # -- wiring
    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = MeshBackend(self.settings)
        return self._backend

    @property
    def available(self) -> bool:
        return self._backend is not None or self.settings.mesh_configured

    def add_sink(self, sink: CallSink) -> None:
        self._sinks.append(sink)

    def model_for(self, tier: str) -> str:
        return self.settings.mesh_model_smart if tier == "smart" else self.settings.mesh_model_fast

    # -- core
    def _emit(self, rec: CallRecord) -> None:
        for sink in self._sinks:
            try:
                sink(rec)
            except Exception:
                log.warning("llm call sink failed", exc_info=True)

    @staticmethod
    def cache_key(
        model: str,
        prompt: Prompt,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        scope: str = "global",
    ) -> str:
        blob = json.dumps(
            {"m": model, "p": prompt.ref, "msg": messages, "params": params},
            sort_keys=True,
            default=str,
        )
        return f"cache:llm:{scope}:" + hashlib.sha256(blob.encode()).hexdigest()

    def _complete(
        self,
        prompt_key: str,
        variables: dict[str, Any],
        *,
        agent: str,
        kind: str,
        user_id: object | None = None,
        session_id: str | None = None,
        tier: str | None = None,
        extra_messages: Sequence[dict[str, Any]] = (),
        use_cache: bool = True,
        metadata: dict[str, Any] | None = None,
        cache_scope: str | None = None,
        **params: Any,
    ) -> LLMResult:
        if not self.available:
            raise LLMUnavailable("Mesh not configured")
        prompt = get_registry().get(prompt_key)
        model = self.model_for(tier or prompt.meta.model_tier)
        messages = prompt.render(**variables) + list(extra_messages)
        masked = self._mask(messages)
        messages = masked[0]
        params.setdefault("temperature", prompt.meta.temperature)

        ttl = prompt.meta.cache_ttl
        scope = cache_scope or (str(user_id) if user_id is not None else "global")
        key = (
            self.cache_key(model, prompt, messages, params, scope)
            if (self.cache and use_cache and ttl)
            else None
        )
        if key and self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                u = hit.get("usage") or {}
                self._emit(
                    CallRecord(
                        agent,
                        model,
                        kind,
                        prompt.meta.name,
                        prompt.meta.version,
                        str(user_id) if user_id else None,
                        cache_layer="L1",
                        saved_cost_usd=estimate_cost(
                            model,
                            u.get("prompt_tokens", 0),
                            u.get("cached_tokens", 0),
                            u.get("completion_tokens", 0),
                        ),
                    )
                )
                return LLMResult(**{**hit, "cache_layer": "L1"})

        tags = [f"agent:{agent}", f"env:{self.settings.app_env}"]
        lf_meta = {
            "agent": agent,
            "prompt_name": prompt.key,
            "prompt_version": prompt.meta.version,
            "prompt_hash": prompt.content_hash[:12],
            **(metadata or {}),
        }
        rec = CallRecord(
            agent,
            model,
            kind,
            prompt.meta.name,
            prompt.meta.version,
            str(user_id) if user_id else None,
        )
        t0 = time.perf_counter()
        try:
            with trace_context(user_id=user_id, session_id=session_id, tags=tags, metadata=lf_meta):
                if masked[1]:
                    score_current_trace(
                        "pii_masked", float(masked[1]), comment=", ".join(sorted(masked[2]))
                    )
                resp = self._call_with_retry(
                    model=model,
                    messages=messages,
                    name=f"{agent}:{prompt.meta.name}",
                    metadata={k: str(v) for k, v in lf_meta.items()},
                    **params,
                )
        except Exception as e:
            rec.ok, rec.error, rec.latency_ms = (
                False,
                type(e).__name__,
                int((time.perf_counter() - t0) * 1000),
            )
            self._emit(rec)
            if isinstance(e, _RETRYABLE):
                raise LLMUnavailable(str(e)) from e
            raise
        rec.latency_ms = int((time.perf_counter() - t0) * 1000)
        result = self._to_result(resp)
        u = result.usage
        rec.prompt_tokens, rec.cached_tokens, rec.completion_tokens = (
            u.get("prompt_tokens", 0),
            u.get("cached_tokens", 0),
            u.get("completion_tokens", 0),
        )
        rec.cost_usd = estimate_cost(
            model, rec.prompt_tokens, rec.cached_tokens, rec.completion_tokens
        )
        self._emit(rec)
        if key and self.cache is not None and not result.tool_calls:
            self.cache.set(
                key, {"text": result.text, "model": result.model, "usage": result.usage}, ttl
            )
        return result

    def _mask(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, set[str]]:
        """PII guardrail: identifiers never leave FinPilot (config/privacy.yml)."""
        if not self.settings.pii_masking:
            return messages, 0, set()
        from finpilot.privacy.pii import mask_messages

        out, counts = mask_messages(messages)
        return out, sum(counts.values()), set(counts)

    def _call_with_retry(self, **kwargs: Any) -> Any:
        @retry(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(max(1, self.settings.mesh_max_retries)),
            wait=wait_exponential_jitter(initial=0.5, max=8),
            reraise=True,
        )
        def _do() -> Any:
            return self.backend.chat(**kwargs)

        return _do()

    @staticmethod
    def _to_result(resp: Any) -> LLMResult:
        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        tool_calls = [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in (getattr(msg, "tool_calls", None) or [])
        ]
        return LLMResult(
            text=msg.content or "",
            model=getattr(resp, "model", "") or "",
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
            },
            tool_calls=tool_calls,
            raw_message={
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": t["id"],
                        "type": "function",
                        "function": {"name": t["name"], "arguments": t["arguments"]},
                    }
                    for t in tool_calls
                ]
                or None,
            },
        )

    # -- public API
    def chat(
        self, prompt_key: str, variables: dict[str, Any], *, agent: str, **kw: Any
    ) -> LLMResult:
        return self._complete(prompt_key, variables, agent=agent, kind="chat", **kw)

    def chat_json(
        self,
        prompt_key: str,
        variables: dict[str, Any],
        schema: type[T],
        *,
        agent: str,
        max_repairs: int = 1,
        **kw: Any,
    ) -> tuple[T, LLMResult]:
        """JSON mode + Pydantic validation; one repair round-trip on invalid output.
        `extra_messages` (e.g. a grounding-guard retry) are appended; they are part of the L1 cache
        key, so retries stay reproducible. Schema-repair turns are never cached."""
        base: list[dict[str, Any]] = list(kw.pop("extra_messages", None) or [])
        extra = base
        last_err: Exception | None = None
        for attempt in range(max_repairs + 1):
            res = self._complete(
                prompt_key,
                variables,
                agent=agent,
                kind="json",
                response_format={"type": "json_object"},
                extra_messages=extra,
                use_cache=attempt == 0,
                **kw,
            )
            try:
                return schema.model_validate_json(_strip_fences(res.text)), res
            except ValidationError as e:
                last_err = e
                extra = [
                    *base,
                    {"role": "assistant", "content": res.text},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "error": "invalid_json_for_schema",
                                "details": e.errors(include_url=False)[:5],
                            },
                            default=str,
                        ),
                    },
                ]
        raise LLMError(f"{prompt_key}: output failed schema validation") from last_err

    def chat_tools(
        self,
        prompt_key: str,
        variables: dict[str, Any],
        tools: list[dict[str, Any]],
        *,
        agent: str,
        history: Sequence[dict[str, Any]] = (),
        **kw: Any,
    ) -> LLMResult:
        return self._complete(
            prompt_key,
            variables,
            agent=agent,
            kind="tools",
            tools=tools,
            extra_messages=history,
            use_cache=False,
            **kw,
        )

    # -- embeddings
    def embeddings_available(self) -> bool:
        if self._embed_ok is None:
            self._embed_ok = self.probe_embeddings()
        return bool(self._embed_ok)

    def probe_embeddings(self) -> bool:
        if not self.available:
            return False
        try:
            vecs = self._embed_raw(["finpilot embeddings probe"], agent="probe", user_id=None)
            return len(vecs) == 1 and len(vecs[0]) > 0
        except Exception as e:
            log.warning("Mesh embeddings unavailable → lexical fallback (%s)", type(e).__name__)
            return False

    def embed(
        self, texts: list[str], *, agent: str, user_id: object | None = None
    ) -> list[list[float]]:
        if not self.embeddings_available():
            raise LLMUnavailable("embeddings disabled/unavailable; use lexical fallback")
        return self._embed_raw(texts, agent=agent, user_id=user_id)

    def _embed_raw(
        self, texts: list[str], *, agent: str, user_id: object | None
    ) -> list[list[float]]:
        model = self.settings.mesh_model_embed
        if self.settings.pii_masking:
            from finpilot.privacy.pii import mask

            texts = [mask(t)[0] for t in texts]
        rec = CallRecord(agent, model, "embed", None, None, str(user_id) if user_id else None)
        t0 = time.perf_counter()
        try:
            with trace_context(user_id=user_id, tags=[f"agent:{agent}"]):
                resp = self.backend.embed(model=model, inputs=texts, name=f"{agent}:embed")
        except Exception as e:
            rec.ok, rec.error = False, type(e).__name__
            rec.latency_ms = int((time.perf_counter() - t0) * 1000)
            self._emit(rec)
            raise
        rec.latency_ms = int((time.perf_counter() - t0) * 1000)
        rec.prompt_tokens = getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0
        rec.cost_usd = estimate_cost(model, rec.prompt_tokens, 0, 0)
        self._emit(rec)
        return [list(d.embedding) for d in sorted(resp.data, key=lambda d: d.index)]


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


# --------------------------------------------------------------------------- singleton
_lock = threading.Lock()
_instance: LLMClient | None = None


def get_llm() -> LLMClient:
    global _instance
    with _lock:
        if _instance is None:
            _instance = LLMClient()
            _wire_defaults(_instance)
        return _instance


def _wire_defaults(client: LLMClient) -> None:
    """Production wiring: Redis L1 cache + `llm_calls` persistence (both optional)."""
    try:
        from finpilot.llm.cache import RedisLLMCache
        from finpilot.redis_client import get_redis

        r = get_redis()
        r.ping()
        client.cache = RedisLLMCache(r)
    except Exception:
        log.warning("Redis unavailable: LLM L1 cache disabled")
    try:
        from finpilot.db.llm_log import db_sink

        client.add_sink(db_sink)
    except Exception:
        log.warning("llm_calls sink unavailable", exc_info=True)


def use_backend(backend: Backend | None, settings: Settings | None = None) -> LLMClient:
    """Replace the process-wide client (tests, eval with recorded responses)."""
    global _instance
    with _lock:
        _instance = LLMClient(settings=settings, backend=backend)
        return _instance
