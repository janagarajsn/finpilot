"""Raw bank descriptor → canonical `merchant_norm` (deterministic; rules in config/normalizer.yml)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from finpilot.config import get_settings


@dataclass(frozen=True)
class _Rules:
    extract: list[re.Pattern[str]]
    strip_prefixes: list[re.Pattern[str]]
    noise: list[re.Pattern[str]]
    aliases: list[tuple[re.Pattern[str], str]]


@lru_cache
def _rules() -> _Rules:
    raw = yaml.safe_load((get_settings().config_dir / "normalizer.yml").read_text(encoding="utf-8"))
    c = lambda p: re.compile(p, re.IGNORECASE)  # noqa: E731
    return _Rules(
        extract=[c(p) for p in raw.get("extract", [])],
        strip_prefixes=[c(p) for p in raw.get("strip_prefixes", [])],
        noise=[c(p) for p in raw.get("noise", [])],
        aliases=[(c(k), v) for k, v in (raw.get("aliases") or {}).items()],
    )


def _squash(s: str) -> str:
    s = re.sub(r"[^A-Z0-9&' ]+", " ", s.upper())
    return re.sub(r"\s+", " ", s).strip(" &'")


def normalize_merchant(raw: str) -> str:
    """Never empty for non-empty input; capped at 80 chars."""
    if not raw or not raw.strip():
        return ""
    r = _rules()
    text_ = re.sub(r"\s+", " ", raw.strip().upper())
    original = text_
    for alias_re, name in r.aliases:  # descriptors that are fully identified up front (ATM etc.)
        if alias_re.match(text_):
            return name
    for pat in r.extract:
        m = pat.search(text_)
        if m and m.group("m").strip():
            text_ = m.group("m")
            break
    for pat in r.strip_prefixes:
        text_ = pat.sub("", text_)
    for pat in r.noise:
        text_ = pat.sub(" ", text_)
    out = _squash(text_)
    for alias_re, name in r.aliases:
        if alias_re.match(out):
            return name
    if not out:  # everything was noise: fall back to the squashed original
        out = _squash(original)[:80]
    return out[:80]
