"""Renders finding titles/bodies from templates/messages/findings.yml (plain text, StrictUndefined)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from finpilot.config import get_settings
from finpilot.insights.models import Finding
from finpilot.money import format_money


@lru_cache
def _templates() -> dict[str, dict[str, str]]:
    path = get_settings().templates_dir / "messages" / "findings.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def _env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    env.globals["money"] = format_money
    return env


def template_types() -> set[str]:
    return set(_templates())


def render(f: Finding) -> Finding:
    t = _templates()[f.type]
    ctx: dict[str, Any] = {**f.data, "f": f}
    f.title = " ".join(_env().from_string(t["title"]).render(**ctx).split())[:300]
    f.body = " ".join(_env().from_string(t["body"]).render(**ctx).split())[:2000]
    return f
