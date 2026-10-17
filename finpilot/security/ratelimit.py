"""Redis fixed-window rate limits (`rl:` namespace).

Fails OPEN if Redis is unreachable (availability over strictness); account lockout lives in
Postgres, so brute force stays bounded even then.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import redis

from finpilot.redis_client import get_redis

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


def hit(
    scope: str, key: str, limit: int, window_s: int, r: redis.Redis | None = None
) -> RateResult:
    now = int(time.time())
    window = now - now % window_s
    k = f"rl:{scope}:{key}:{window}"
    try:
        client = r or get_redis()
        pipe = client.pipeline()
        pipe.incr(k)
        pipe.expire(k, window_s + 1)
        count = int(pipe.execute()[0])
    except redis.RedisError:
        log.warning("rate limiter unavailable (fail-open) for %s", scope)
        return RateResult(True, limit, 0)
    retry = window + window_s - now
    return RateResult(count <= limit, max(limit - count, 0), retry if count > limit else 0)


def enforce(scope: str, key: str, limit: int, window_s: int) -> None:
    res = hit(scope, key, limit, window_s)
    if not res.allowed:
        raise RateLimited(res.retry_after)
