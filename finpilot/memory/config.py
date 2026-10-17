"""config/memory.yml loader."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import yaml

from finpilot.config import get_settings


@lru_cache
def config() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(
        (get_settings().config_dir / "memory.yml").read_text(encoding="utf-8")
    )
    raw["_blocked"] = [re.compile(p) for p in raw.get("blocked_patterns") or []]
    return raw


def blocked(text: str) -> bool:
    """Account/card numbers, credentials, one-time codes: never stored."""
    return any(p.search(text) for p in config()["_blocked"])


def tokens(text: str) -> int:
    return max(1, len(text) // 4)
