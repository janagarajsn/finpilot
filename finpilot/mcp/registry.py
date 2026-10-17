"""Loads `config/mcp_servers.yml` (with ${ENV} expansion) into typed server configs."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from finpilot.config import get_settings

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class MCPServerConfig(BaseModel):
    name: str
    enabled: bool = True
    transport: Literal["streamable_http", "stdio"] = "streamable_http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    timeout_s: float = 5.0
    retries: int = 2
    breaker_failures: int = 3
    breaker_reset_s: float = 30.0
    cache_ttl_s: int = 0
    allowed_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerConfig:
        if self.transport == "streamable_http" and not self.url:
            raise ValueError(f"MCP server {self.name}: url required for streamable_http")
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"MCP server {self.name}: command required for stdio")
        return self


def _expand(value: str) -> str:
    settings = get_settings()

    def repl(m: re.Match[str]) -> str:
        var, default = m.group(1), m.group(2)
        env = os.getenv(var)
        if env:
            return env
        from_settings = getattr(settings, var.lower(), None)
        if isinstance(from_settings, str) and from_settings:
            return from_settings
        return default or ""

    return _ENV.sub(repl, value)


def load_registry(path: Path | None = None) -> dict[str, MCPServerConfig]:
    path = path or get_settings().config_dir / "mcp_servers.yml"
    raw = yaml.safe_load(path.read_text()) or {}
    out: dict[str, MCPServerConfig] = {}
    for name, cfg in (raw.get("servers") or {}).items():
        cfg = {k: _expand(v) if isinstance(v, str) else v for k, v in cfg.items()}
        out[name] = MCPServerConfig(name=name, **cfg)
    return out


@lru_cache
def get_registry() -> dict[str, MCPServerConfig]:
    return load_registry()
