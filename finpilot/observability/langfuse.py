"""Langfuse (OTel-based SDK) initialization. No-ops gracefully when unconfigured.

The SDK keeps a process-wide singleton; constructing `Langfuse(...)` once here makes
`get_client()`, `@observe` and the `langfuse.openai` wrapper all use our settings + masking.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

from langfuse import Langfuse, get_client, propagate_attributes

from finpilot.config import Settings, get_settings
from finpilot.observability.masking import hash_user_id, make_mask

log = logging.getLogger(__name__)
_client: Langfuse | None = None


def init_langfuse(settings: Settings | None = None) -> Langfuse:
    global _client
    if _client is not None:
        return _client
    s = settings or get_settings()
    if s.langfuse_configured:
        _client = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key.get_secret_value() if s.langfuse_secret_key else None,
            host=s.langfuse_host,
            environment=s.langfuse_environment,
            mask=make_mask(s.langfuse_capture_content),
        )
        log.info("Langfuse tracing enabled → %s", s.langfuse_host)
    else:
        _client = Langfuse(tracing_enabled=False, public_key="disabled", secret_key="disabled")  # noqa: S106 — placeholder for disabled no-op client
        log.info("Langfuse not configured: tracing disabled (no-op)")
    return _client


def langfuse_enabled() -> bool:
    return get_settings().langfuse_configured


def client() -> Langfuse:
    init_langfuse()
    return get_client()


@contextmanager
def trace_context(
    *,
    user_id: object | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    trace_name: str | None = None,
) -> Iterator[None]:
    """Propagate user/session/tags/metadata to every observation created inside."""
    init_langfuse()
    md = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
    cm: AbstractContextManager[Any]
    try:
        cm = propagate_attributes(
            user_id=hash_user_id(user_id) if user_id is not None else None,
            session_id=session_id,
            tags=tags,
            metadata=md or None,
            trace_name=trace_name,
        )
    except Exception:  # never let observability break the app
        log.debug("propagate_attributes failed", exc_info=True)
        cm = nullcontext()
    with cm:
        yield


def score_current_trace(name: str, value: float | str, comment: str | None = None) -> None:
    if not langfuse_enabled():
        return
    try:
        client().score_current_trace(name=name, value=value, comment=comment)
    except Exception:
        log.debug("score_current_trace failed", exc_info=True)


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            log.debug("langfuse flush failed", exc_info=True)


@contextmanager
def agent_span(
    name: str,
    *,
    user_id: object | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[str | None]:
    """One Langfuse span (= one trace) around an agent run; yields the trace id (None when disabled)."""
    if not langfuse_enabled():
        yield None
        return
    try:
        cm: AbstractContextManager[Any] = client().start_as_current_observation(
            name=name, as_type="span"
        )
    except Exception:
        log.debug("agent_span failed", exc_info=True)
        cm = nullcontext()
    with (
        cm,
        trace_context(
            user_id=user_id,
            session_id=session_id,
            tags=[f"agent:{name}"],
            metadata=metadata,
            trace_name=name,
        ),
    ):
        try:
            tid = client().get_current_trace_id()
        except Exception:
            tid = None
        yield tid
