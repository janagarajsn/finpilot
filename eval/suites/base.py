"""Suite plumbing shared by all eval suites."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Metric:
    metric: str
    value: float
    unit: str | None = None
    dimension: dict[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    case_id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    actual: dict[str, Any]
    passed: bool
    score: float | None = None
    error_type: str | None = None
    langfuse_trace_id: str | None = None


@dataclass
class SuiteResult:
    suite: str
    metrics: list[Metric] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    details: dict[str, Any] = field(
        default_factory=dict
    )  # tables for the UI (per-category, confusions...)

    def headline(self) -> dict[str, float]:
        return {
            m.metric: m.value
            for m in self.metrics
            if m.dimension.get("scope") == "overall" and m.dimension.get("primary")
        }


@dataclass
class SuiteContext:
    mode: str  # offline | full
    run_tag: str
    personas: list[str] | None = None
    llm: Any = None  # LLMClient (full mode)
