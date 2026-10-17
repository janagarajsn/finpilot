"""Response schemas referenced by prompt frontmatter (`response_schema`)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HelloResponse(BaseModel):
    ok: bool
    echo: str


class CategorizeItem(BaseModel):
    id: str = Field(max_length=20)
    category: str = Field(max_length=80)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(default="", max_length=300)


class CategorizeResponse(BaseModel):
    results: list[CategorizeItem]


class ParsedScenario(BaseModel):
    """Simulator NL parser output. Code (not the LLM) checks required slots and builds a Scenario."""

    intent: Literal[
        "one_time_purchase",
        "recurring_cost",
        "cancel_subscription",
        "income_change",
        "savings_transfer",
        "category_cut",
        "loan_or_installments",
        "unsupported",
    ]
    amount: float | None = None
    currency: str | None = Field(default=None, max_length=3)
    date: str | None = Field(default=None, max_length=10)
    frequency: Literal["once", "weekly", "monthly", "yearly"] | None = None
    occurrences: int | None = Field(default=None, ge=1, le=520)
    n: int | None = Field(default=None, ge=1, le=120)
    apr: float | None = Field(default=None, ge=0, le=100)
    pct: float | None = Field(default=None, ge=-100, le=500)
    category: str | None = Field(default=None, max_length=80)
    merchant: str | None = Field(default=None, max_length=80)
    label: str | None = Field(default=None, max_length=40)
    reason: str | None = Field(default=None, max_length=300)


class AdviceItem(BaseModel):
    recommendation_id: str = Field(max_length=12)
    title: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=400)
    why: str = Field(min_length=1, max_length=800)
    cited_finding_ids: list[str] = Field(default_factory=list, max_length=8)
    numbers_used: list[str] = Field(default_factory=list, max_length=16)


class AdviceResponse(BaseModel):
    recommendations: list[AdviceItem] = Field(max_length=3)


class NarrationResponse(BaseModel):
    answer: str = Field(min_length=1, max_length=900)


class JudgeScore(BaseModel):
    recommendation_id: str = Field(max_length=64)
    specificity: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)
    grounded: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    overall: int = Field(ge=1, le=5)
    rationale: str = Field(default="", max_length=400)


class JudgeResponse(BaseModel):
    scores: list[JudgeScore] = Field(max_length=10)


class IntentResponse(BaseModel):
    intent: str = Field(max_length=40)
    period: str | None = Field(default=None, max_length=20)
    confidence: float = Field(default=0.5, ge=0, le=1)


class SummaryResponse(BaseModel):
    summary: str = Field(max_length=2000)


class MemoryCandidate(BaseModel):
    kind: Literal["fact", "preference", "goal_context", "decision", "episode"]
    topic: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=300)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    expires_on: str | None = Field(default=None, max_length=10)  # ISO date for time-bound facts


class MemoryExtraction(BaseModel):
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=10)


class DigestPhrase(BaseModel):
    insight: str = Field(min_length=1, max_length=300)
    action: str = Field(min_length=1, max_length=350)
