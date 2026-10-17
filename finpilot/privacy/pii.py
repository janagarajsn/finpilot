"""PII detection and masking with Presidio (rules: config/privacy.yml).

The analyzer runs on a tokenizer-only spaCy pipeline (`spacy.blank("en")`): every recognizer we
use is pattern/checksum based, so no 500 MB language model is needed and nothing is downloaded.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import spacy
import yaml
from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts, SpacyNlpEngine
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    InAadhaarRecognizer,
    InPanRecognizer,
    PhoneRecognizer,
    UsSsnRecognizer,
)

CONFIG = Path(__file__).resolve().parents[2] / "config" / "privacy.yml"
_MAYBE_PII = re.compile(r"[\d@]")  # every entity we detect has a digit or an @: skip the rest fast


@lru_cache
def config() -> dict[str, Any]:
    return dict(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


class _BlankSpacy(SpacyNlpEngine):
    """Tokenizer only: context words still work for Presidio, no model download."""

    def __init__(self) -> None:
        super().__init__(models=[{"lang_code": "en", "model_name": "blank:en"}])

    def load(self) -> None:
        self.nlp = {"en": spacy.blank("en")}  # type: ignore[assignment]


class _GroupPattern(EntityRecognizer):
    """Regex recognizer that reports one capture group (so 'account no' stays readable)."""

    def __init__(self, entity: str, pattern: str, group: int, score: float) -> None:
        self.regex, self.group, self.score = re.compile(pattern), group, score
        super().__init__(supported_entities=[entity], name=f"{entity}_custom")

    def load(self) -> None:
        return None

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        return [
            RecognizerResult(
                self.supported_entities[0], m.start(self.group), m.end(self.group), self.score
            )
            for m in self.regex.finditer(text)
        ]


@lru_cache
def analyzer() -> AnalyzerEngine:
    cfg = config()
    reg = RecognizerRegistry(supported_languages=["en"])
    for r in (
        CreditCardRecognizer(),
        EmailRecognizer(),
        PhoneRecognizer(supported_regions=tuple(cfg["phone_regions"])),
        IbanRecognizer(),
        InPanRecognizer(),
        InAadhaarRecognizer(),
        UsSsnRecognizer(),
    ):
        reg.add_recognizer(r)
    for entity, c in cfg["custom"].items():
        reg.add_recognizer(_GroupPattern(entity, c["pattern"], int(c["group"]), float(c["score"])))
    nlp = _BlankSpacy()
    nlp.load()
    return AnalyzerEngine(registry=reg, nlp_engine=nlp, supported_languages=["en"])


@dataclass(frozen=True)
class Span:
    entity: str
    start: int
    end: int
    score: float


def find(text: str) -> list[Span]:
    """Non-overlapping PII spans (higher score, then longer span, wins an overlap)."""
    if not text or not _MAYBE_PII.search(text):
        return []
    cfg = config()
    hits = analyzer().analyze(
        text,
        language="en",
        entities=list(cfg["placeholders"]),
        score_threshold=float(cfg["score_threshold"]),
    )
    out: list[Span] = []
    for h in sorted(hits, key=lambda h: (-h.score, -(h.end - h.start), h.start)):
        if all(h.end <= s.start or h.start >= s.end for s in out):
            out.append(Span(h.entity_type, h.start, h.end, float(h.score)))
    return sorted(out, key=lambda s: s.start)


def mask(text: str) -> tuple[str, Counter[str]]:
    spans = find(text)
    counts: Counter[str] = Counter(s.entity for s in spans)
    ph = config()["placeholders"]
    for s in reversed(spans):
        text = text[: s.start] + ph[s.entity] + text[s.end :]
    return text, counts


def mask_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Chat messages with every string `content` masked (tool-call structure untouched)."""
    total: Counter[str] = Counter()
    out = copy.deepcopy(messages)
    for m in out:
        c = m.get("content")
        if isinstance(c, str):
            m["content"], n = mask(c)
            total += n
        elif isinstance(c, list):  # multi-part content
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"], n = mask(part["text"])
                    total += n
    return out, total


def leaks(text: str) -> list[str]:
    """Entity types present in an outgoing answer (empty = clean)."""
    return sorted({s.entity for s in find(text)})
