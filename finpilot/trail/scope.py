"""Advice-scope guardrail: no specific investment, tax or legal directives (config/guardrails.yml)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config" / "guardrails.yml"


@lru_cache
def _rules() -> list[tuple[str, str, list[re.Pattern[str]]]]:
    cfg: dict[str, Any] = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return [
        (kind, str(r["message"]), [re.compile(p) for p in r["patterns"]])
        for kind, r in cfg["directives"].items()
    ]


def violations(text: str) -> list[tuple[str, str]]:
    """[(kind, message for the retry)] for every directive kind the text crosses."""
    low = (text or "").lower()
    return [(k, msg) for k, msg, pats in _rules() if any(p.search(low) for p in pats)]
