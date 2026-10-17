"""Persists `CallRecord`s from the LLM client into `llm_calls` (admin role; log table)."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from finpilot.config import get_settings
from finpilot.llm.client import CallRecord, LLMClient

log = logging.getLogger(__name__)

_INSERT = text("""
INSERT INTO llm_calls (user_id, agent, kind, model, prompt_name, prompt_version, prompt_tokens,
  cached_tokens, completion_tokens, latency_ms, cost_usd, cache_layer, ok, error)
VALUES (CAST(:user_id AS uuid), :agent, :kind, :model, :prompt_name, :prompt_version,
  :prompt_tokens, :cached_tokens, :completion_tokens, :latency_ms, :cost_usd, :cache_layer, :ok, :error)
""")


@lru_cache
def _engine() -> Engine:
    return create_engine(get_settings().database_admin_url, pool_pre_ping=True, pool_size=2)


def db_sink(rec: CallRecord) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(_INSERT, {k: v for k, v in rec.__dict__.items() if k != "saved_cost_usd"})
    except Exception:
        log.warning("failed to persist llm_call", exc_info=True)


def install(client: LLMClient) -> None:
    client.add_sink(db_sink)
