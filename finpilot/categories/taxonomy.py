"""Loads `config/taxonomy.yml` (the system category taxonomy)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from finpilot.config import get_settings

FlowType = Literal["income", "expense", "transfer", "savings"]
Nature = Literal["fixed", "essential_variable", "discretionary", "n/a"]


class TaxonomyEntry(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    flow_type: FlowType
    nature: Nature
    description: str
    examples: list[str] = []
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str | None = None


@lru_cache
def load_taxonomy() -> list[TaxonomyEntry]:
    raw = yaml.safe_load((get_settings().config_dir / "taxonomy.yml").read_text(encoding="utf-8"))
    entries = [TaxonomyEntry(**c) for c in raw["categories"]]
    slugs = [e.slug for e in entries]
    names = [e.name.lower() for e in entries]
    if len(set(slugs)) != len(slugs) or len(set(names)) != len(names):
        raise ValueError("taxonomy.yml: duplicate slug or name")
    return entries
