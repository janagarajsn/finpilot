"""Process-wide Redis client (DB 0: app state/cache). Keys are namespaced; see CLAUDE.md."""

from __future__ import annotations

from functools import lru_cache

import redis

from finpilot.config import get_settings


@lru_cache
def get_redis(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(
        url or get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2
    )
