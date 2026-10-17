"""Versioned prompt files: `prompts/<agent>/<name>.v<N>.md`.

Each file = YAML frontmatter + Jinja2 body split by `{# STATIC #}` / `{# DYNAMIC #}` markers.
The STATIC part is rendered as the system message and always comes first so providers can
reuse the cached prefix; the DYNAMIC part (request data) becomes the user message.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from jinja2 import Environment, StrictUndefined, meta
from pydantic import BaseModel, Field

from finpilot.config import get_settings

STATIC_MARK = "{# STATIC #}"
DYNAMIC_MARK = "{# DYNAMIC #}"
_FILE_RE = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>\d+)\.md$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class PromptError(ValueError):
    pass


class PromptMeta(BaseModel):
    name: str
    version: int
    model_tier: Literal["fast", "smart"] = "fast"
    temperature: float = 0.0
    response_schema: str | None = None
    input_variables: list[str] = Field(default_factory=list)
    cache_ttl: int = 0


@dataclass(frozen=True)
class Prompt:
    agent: str
    meta: PromptMeta
    static_src: str
    dynamic_src: str
    content_hash: str
    path: Path
    _env: Environment = field(repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.agent}/{self.meta.name}"

    @property
    def ref(self) -> str:
        """Stable identifier for tracing and cache keys."""
        return f"{self.key}@v{self.meta.version}#{self.content_hash[:12]}"

    def render(self, **variables: Any) -> list[dict[str, str]]:
        missing = [v for v in self.meta.input_variables if v not in variables]
        if missing:
            raise PromptError(f"{self.ref}: missing input variables {missing}")
        extra = set(variables) - set(self.meta.input_variables)
        if extra:
            raise PromptError(f"{self.ref}: unexpected variables {sorted(extra)}")
        messages = [
            {
                "role": "system",
                "content": self._env.from_string(self.static_src).render(**variables).strip(),
            }
        ]
        dynamic = self._env.from_string(self.dynamic_src).render(**variables).strip()
        if dynamic:
            messages.append({"role": "user", "content": dynamic})
        return messages


def _make_env() -> Environment:
    # Prompts are plain text sent to an LLM, not HTML; autoescape would corrupt them.
    return Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)  # noqa: S701


def load_prompt_file(path: Path, agent: str) -> Prompt:
    m = _FILE_RE.match(path.name)
    if not m:
        raise PromptError(f"bad prompt filename {path.name}; expected <name>.v<N>.md")
    raw = path.read_text(encoding="utf-8")
    fm = _FRONTMATTER_RE.match(raw)
    if not fm:
        raise PromptError(f"{path}: missing YAML frontmatter")
    meta_obj = PromptMeta.model_validate(yaml.safe_load(fm.group(1)) or {})
    if meta_obj.name != m.group("name") or meta_obj.version != int(m.group("version")):
        raise PromptError(f"{path}: frontmatter name/version do not match filename")
    body = fm.group(2)
    if STATIC_MARK not in body:
        raise PromptError(f"{path}: body must contain {STATIC_MARK}")
    after_static = body.split(STATIC_MARK, 1)[1]
    static_src, _, dynamic_src = after_static.partition(DYNAMIC_MARK)

    env = _make_env()
    used = meta.find_undeclared_variables(env.parse(static_src + dynamic_src))
    undeclared = used - set(meta_obj.input_variables)
    if undeclared:
        raise PromptError(f"{path}: template uses undeclared variables {sorted(undeclared)}")
    unused = set(meta_obj.input_variables) - used
    if unused:
        raise PromptError(f"{path}: declared but unused variables {sorted(unused)}")

    return Prompt(
        agent=agent,
        meta=meta_obj,
        static_src=static_src,
        dynamic_src=dynamic_src,
        content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        path=path,
        _env=env,
    )


class PromptRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._prompts: dict[str, dict[int, Prompt]] = {}
        self.reload()

    def reload(self) -> None:
        self._prompts.clear()
        if not self.root.exists():
            raise PromptError(f"prompts dir not found: {self.root}")
        for path in sorted(self.root.glob("*/*.md")):
            p = load_prompt_file(path, agent=path.parent.name)
            self._prompts.setdefault(p.key, {})[p.meta.version] = p

    def get(self, key: str, version: int | None = None) -> Prompt:
        """`key` is `<agent>/<name>`; latest version unless `version` is given."""
        versions = self._prompts.get(key)
        if not versions:
            raise PromptError(f"unknown prompt {key}")
        if version is None:
            return versions[max(versions)]
        if version not in versions:
            raise PromptError(f"unknown prompt version {key}@v{version}")
        return versions[version]

    def all(self) -> list[Prompt]:
        return [p for vs in self._prompts.values() for p in vs.values()]


@lru_cache
def get_registry() -> PromptRegistry:
    return PromptRegistry(get_settings().prompts_dir)
