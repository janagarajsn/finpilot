"""Renders deterministic message templates from `templates/messages/` (emails, digests,
degraded-mode text). Text-only output: no HTML autoescaping."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from finpilot.config import get_settings


@lru_cache
def _env() -> Environment:
    return Environment(  # noqa: S701 — plain-text messages, not HTML
        loader=FileSystemLoader(str(get_settings().templates_dir / "messages")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_message(template: str, /, **ctx: Any) -> str:
    return _env().get_template(template).render(**ctx)


@lru_cache
def _snippets(stem: str) -> dict[str, Any]:
    import yaml

    path = get_settings().templates_dir / "messages" / f"{stem}.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def _sandbox() -> Any:
    from jinja2.sandbox import SandboxedEnvironment

    from finpilot.money import format_money

    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)  # plain text
    env.globals["money"] = format_money
    return env


def render_snippet(stem: str, path: str, /, **ctx: Any) -> str:
    """Render templates/messages/<stem>.yml at a dotted `path` (e.g. "verdict.risky"), whitespace-collapsed."""
    node: Any = _snippets(stem)
    for part in path.split("."):
        node = node[part]
    return " ".join(_sandbox().from_string(node).render(**ctx).split())
