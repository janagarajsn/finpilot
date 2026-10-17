"""Redis locks for scheduler ticks (`lock:beat:*`): a tick never runs twice concurrently."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

from finpilot.redis_client import get_redis

log = logging.getLogger(__name__)
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@contextmanager
def redis_lock(name: str, ttl_s: int = 600) -> Iterator[bool]:
    """Yields True if acquired. Released only by its owner (token check)."""
    key, token = f"lock:{name}", secrets.token_hex(8)
    r = get_redis()
    got = bool(r.set(key, token, nx=True, ex=ttl_s))
    try:
        yield got
    finally:
        if got:
            try:
                r.eval(_RELEASE, 1, key, token)
            except Exception:
                log.warning("lock release failed for %s", key, exc_info=True)
