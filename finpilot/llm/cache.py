"""L1 exact LLM response cache in Redis (`cache:llm:{scope}:{hash}`).

Key = scope + hash(model, prompt name/version/content-hash, rendered messages, params).
`scope` is the user id in the product (keys are never shared across users) and a stable
persona/suite name in evals, which makes eval runs reproducible and cheap.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

log = logging.getLogger(__name__)


class RedisLLMCache:
    def __init__(self, client: redis.Redis) -> None:
        self.r = client

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self.r.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            log.debug("llm cache get failed", exc_info=True)
            return None

    def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            self.r.set(key, json.dumps(value, default=str), ex=max(1, int(ttl)))
        except Exception:
            log.debug("llm cache set failed", exc_info=True)
