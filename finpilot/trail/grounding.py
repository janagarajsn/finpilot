"""Grounding guard: every number, percentage, date and amount in LLM text must come from the
computed-facts set supplied with the prompt (BUILD_PROMPT §6, Amendment: FX facts).

Extraction understands ₹ / Rs / INR / $ / USD (prefix or suffix), Indian (1,25,000) and Western
(125,000) grouping, "1.5 lakh", "2 crore", "80k", percentages, ISO and written dates
("12 Mar", "March 12, 2026", "Mar 2026"). Matching is CURRENCY-AWARE: "₹1,712" passes only if an
INR fact of about 1,712 exists, so the model cannot convert currencies itself: a converted figure
is accepted only when that converted figure is a fact. Tolerance: max(1% of the fact, the
currency's grounding_abs_tolerance), and the precision the text was written with ("5×" ≈ 5.3).

`guarded_json` runs generate → verify (+ caller validation) → regenerate with the problems named
(max 2 retries) → deterministic fallback, and returns a report for the decision trail.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from pydantic import BaseModel

from finpilot.money import get_currency_registry
from finpilot.privacy.pii import leaks as pii_leaks

REL_TOL = Decimal("0.01")
NAME_KEYS = {
    "merchant",
    "category",
    "name",
    "label",
    "goal",
    "purchase",
    "group_label",
    "names",
    "overlap_with",
}
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
SCALE = r"(?:\s?(?:k\b|lakhs?\b|lacs?\b|crores?\b|cr\b|mn\b|million\b))?"
CUR_PREFIX = r"(?:₹|rs\.?|inr|us\$|\$|usd)"
CUR_SUFFIX = r"(?:inr|rupees?|usd|dollars?)\b"
SCALES = {
    "k": Decimal(1_000),
    "lakh": Decimal(100_000),
    "lakhs": Decimal(100_000),
    "lac": Decimal(100_000),
    "lacs": Decimal(100_000),
    "crore": Decimal(10_000_000),
    "crores": Decimal(10_000_000),
    "cr": Decimal(10_000_000),
    "mn": Decimal(1_000_000),
    "million": Decimal(1_000_000),
}
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("date", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    (
        "date",
        re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b", re.I),
    ),
    (
        "date",
        re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I),
    ),
    ("month", re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{4}})\b", re.I)),
    ("money", re.compile(rf"(?<![\w.])(-|−)?\s?({CUR_PREFIX})\s?(-|−)?({NUM}){SCALE}", re.I)),
    ("money", re.compile(rf"(?<![\w.])(-|−)?({NUM}){SCALE}\s?({CUR_SUFFIX})", re.I)),
    (
        "percent",
        re.compile(
            rf"(?<![\w.])(-|−|\+)?({NUM})\s?(%|percent\b|per cent\b|pts?\b|percentage points?\b)",
            re.I,
        ),
    ),
    ("number", re.compile(rf"(?<![\w.])(-|−)?({NUM}){SCALE}(?:st|nd|rd|th|x|×)?", re.I)),
]


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _scale(text: str) -> Decimal:
    m = re.search(r"(k|lakhs?|lacs?|crores?|cr|mn|million)\b", text, re.I)
    return SCALES[m.group(1).lower()] if m else Decimal(1)


def _currency(token: str) -> str:
    t = token.lower().rstrip(".")
    return "INR" if t in ("₹", "rs", "inr", "rupee", "rupees") else "USD"


def _decimals(num: str) -> int:
    return len(num.split(".")[1]) if "." in num else 0


@dataclass(frozen=True)
class Mention:
    text: str
    kind: str  # money | percent | number | date | month
    value: Decimal | None = None
    currency: str | None = None
    precision: Decimal = Decimal(0)  # half-unit of the written precision × scale
    date: tuple[int | None, int, int | None] | None = None  # (year?, month, day?)


def extract(text: str) -> list[Mention]:
    """All numeric mentions, each character consumed once (dates before amounts before numbers)."""
    taken = [False] * len(text)
    out: list[Mention] = []
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            if any(taken[m.start() : m.end()]):
                continue
            g = m.groups()
            mention: Mention | None = None
            if kind == "date":
                if g[0].isdigit() and len(g[0]) == 4:  # ISO
                    mention = Mention(m.group(0), "date", date=(int(g[0]), int(g[1]), int(g[2])))
                elif g[0].isdigit():
                    mention = Mention(
                        m.group(0),
                        "date",
                        date=(int(g[2]) if g[2] else None, MONTHS[g[1][:3].lower()], int(g[0])),
                    )
                else:
                    mention = Mention(
                        m.group(0),
                        "date",
                        date=(int(g[2]) if g[2] else None, MONTHS[g[0][:3].lower()], int(g[1])),
                    )
                if not 1 <= (mention.date[2] or 1) <= 31:  # type: ignore[index]
                    continue
            elif kind == "month":
                mention = Mention(
                    m.group(0), "month", date=(int(g[1]), MONTHS[g[0][:3].lower()], None)
                )
            elif kind == "money":
                if g[1] is not None and re.fullmatch(CUR_PREFIX, g[1], re.I):  # prefix form
                    neg, cur, neg2, num = g[0] or g[2], g[1], g[2], g[3]
                    del neg2
                else:
                    neg, num, cur = g[0], g[1], g[2]
                sc = _scale(m.group(0))
                v = _dec(num) * sc
                mention = Mention(
                    m.group(0),
                    "money",
                    -v if neg else v,
                    _currency(cur),
                    Decimal(5).scaleb(-_decimals(num) - 1) * sc,
                )
            elif kind == "percent":
                v = _dec(g[1])
                mention = Mention(
                    m.group(0),
                    "percent",
                    -v if g[0] in ("-", "−") else v,
                    precision=Decimal(5).scaleb(-_decimals(g[1]) - 1),
                )
            else:
                sc = _scale(m.group(0))
                v = _dec(g[1]) * sc
                mention = Mention(
                    m.group(0),
                    "number",
                    -v if g[0] else v,
                    precision=Decimal(5).scaleb(-_decimals(g[1]) - 1) * sc,
                )
            for i in range(m.start(), m.end()):
                taken[i] = True
            out.append(mention)
    return out


# ---------------------------------------------------------------------------- facts
@dataclass
class FactSet:
    money: list[tuple[Decimal, str]] = field(default_factory=list)  # (value, currency)
    numbers: list[Decimal] = field(default_factory=list)
    dates: set[dt.date] = field(default_factory=set)
    labels: dict[str, Any] = field(default_factory=dict)  # for the prompt / trail
    literals: set[str] = field(
        default_factory=set
    )  # names whose digits are not claims ("LYFT RIDE SAT 11PM")

    def add_literal(self, value: Any) -> FactSet:
        if isinstance(value, str) and re.search(r"\d", value) and len(value.strip()) >= 3:
            self.literals.add(value.strip())
        return self

    def add_money(self, value: Any, currency: str, label: str | None = None) -> FactSet:
        v = _to_dec(value)
        if v is not None:
            self.money.append((v, currency.upper()))
            if label:
                self.labels[label] = f"{v} {currency.upper()}"
        return self

    def add_number(self, value: Any, label: str | None = None) -> FactSet:
        v = _to_dec(value)
        if v is not None:
            self.numbers.append(v)
            if label:
                self.labels[label] = str(v)
        return self

    def add_date(self, value: Any) -> FactSet:
        d = _to_date(value)
        if d:
            self.dates.add(d)
        return self

    def add_obj(self, obj: Any, currency: str, _key: str = "") -> FactSet:
        """Walk JSON-like data. Decimals and numeric strings are money in the nearest currency
        (keys under an `orig_currency` dict use it); ints/floats are plain numbers; ISO dates are dates."""
        if isinstance(obj, BaseModel):
            obj = obj.model_dump()
        if isinstance(obj, dict):
            cur = str(obj.get("currency") or currency).upper()
            orig = str(obj.get("orig_currency") or cur).upper()
            for k, v in obj.items():
                if k in NAME_KEYS:
                    self.add_literal(v)
                key_cur = (
                    orig
                    if k in ("baseline", "observed", "orig_amount", "amount", "from")
                    and obj.get("orig_currency")
                    else cur
                )
                if isinstance(obj.get(f"{k}_currency"), str):  # e.g. charge + charge_currency
                    key_cur = obj[f"{k}_currency"].upper()
                self.add_obj(v, key_cur if len(key_cur) == 3 else currency, k)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                self.add_obj(v, currency, _key)
        elif isinstance(obj, bool) or obj is None:
            return self
        elif isinstance(obj, Decimal):
            self.add_money(obj, currency)
            self.add_number(obj)
        elif isinstance(obj, (int, float)):
            self.add_number(obj)
        elif isinstance(obj, (dt.date, dt.datetime)):
            self.add_date(obj)
        elif isinstance(obj, str):
            if _to_date(obj):
                self.add_date(obj)
            elif ml := re.fullmatch(
                rf"({MONTH_RE})\.?\s+(\d{{4}})", obj.strip(), re.I
            ):  # "October 2025"
                self.add_date(dt.date(int(ml.group(2)), MONTHS[ml.group(1)[:3].lower()], 1))
            elif re.fullmatch(r"-?\d+(\.\d+)?", obj.strip()):
                self.add_money(obj, currency)
                self.add_number(obj)
        return self


def _to_dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _to_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
        try:
            return dt.date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------- verification
@dataclass
class GroundingReport:
    verified: list[dict[str, Any]] = field(default_factory=list)
    unverified: list[dict[str, Any]] = field(default_factory=list)
    pii: list[str] = field(
        default_factory=list
    )  # identifier types found in the text (must be empty)

    @property
    def total(self) -> int:
        return len(self.verified) + len(self.unverified)

    @property
    def passed(self) -> bool:
        return not self.unverified and not self.pii

    @property
    def ratio(self) -> float:
        return 1.0 if self.total == 0 else len(self.verified) / self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "unverified": self.unverified,
            "passed": self.passed,
            "grounded_number_ratio": round(self.ratio, 4),
            "pii_leaks": self.pii,
        }


def _abs_tol(currency: str | None) -> Decimal:
    reg = get_currency_registry()
    if currency and reg.is_supported(currency):
        return reg.get(currency).thresholds.grounding_abs_tolerance
    return Decimal(1)


def _close(v: Decimal, f: Decimal, abs_tol: Decimal, precision: Decimal) -> bool:
    return abs(abs(v) - abs(f)) <= max(abs(f) * REL_TOL, abs_tol, precision)


def verify_mention(m: Mention, facts: FactSet) -> bool:
    if m.kind in ("date", "month"):
        y, mo, d = m.date  # type: ignore[misc]
        return any(
            (y is None or f.year == y) and f.month == mo and (d is None or f.day == d)
            for f in facts.dates
        )
    assert m.value is not None
    if m.kind == "money":
        pool = [f for f, c in facts.money if c == m.currency]
        return any(_close(m.value, f, _abs_tol(m.currency), m.precision) for f in pool)
    if m.kind == "percent":
        return any(
            _close(m.value, f, Decimal("0.5"), m.precision)
            or (abs(f) <= 1 and _close(m.value, f * 100, Decimal("0.5"), m.precision))
            for f in facts.numbers
        )
    # plain number: a fact number, an amount in any currency, or a day/year of a fact date
    if any(_close(m.value, f, Decimal("0"), m.precision) for f in facts.numbers):
        return True
    if any(
        _close(m.value, f, Decimal("0"), max(m.precision, Decimal("0.5"))) for f, _ in facts.money
    ):
        return True
    iv = int(m.value) if m.value == m.value.to_integral_value() else None
    return iv is not None and any(iv in (f.day, f.year) for f in facts.dates)


def check(texts: Iterable[str], facts: FactSet) -> GroundingReport:
    rep = GroundingReport()
    for t in texts:
        t = t or ""
        rep.pii = sorted({*rep.pii, *pii_leaks(t)})  # output guardrail: no identifiers, ever
        for lit in sorted(facts.literals, key=len, reverse=True):  # mask known names first
            t = re.sub(re.escape(lit), " " * len(lit), t, flags=re.I)
        for m in extract(t):
            item = {"text": m.text.strip(), "kind": m.kind}
            (rep.verified if verify_mention(m, facts) else rep.unverified).append(item)
    return rep


# ---------------------------------------------------------------------------- guarded generation
@dataclass
class GuardResult:
    report: GroundingReport
    attempts: int
    first_attempt_pass: bool
    fallback_used: bool
    problems: list[list[str]]  # per attempt
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.report.as_dict(),
            "attempts": self.attempts,
            "retries": max(self.attempts - 1, 0),
            "first_attempt_pass": self.first_attempt_pass,
            "fallback_used": self.fallback_used,
            "problems": self.problems,
            "model": self.model,
        }


# A TypeVar, not PEP 695 `def f[T: ...]`: the hackathon's CI compiles every file with Python 3.11.
T = TypeVar("T", bound=BaseModel)


def guarded_json(  # noqa: UP047
    llm: Any,
    prompt_key: str,
    variables: dict[str, Any],
    schema: type[T],
    *,
    facts: FactSet,
    texts: Callable[[T], Sequence[str]],
    fallback: Callable[[], T],
    validate: Callable[[T], list[str]] | None = None,
    max_retries: int = 2,
    agent: str,
    **kw: Any,
) -> tuple[T, GuardResult]:
    """Generate → ground (+ validate) → retry with the problems named → fallback template."""
    from finpilot.llm.client import LLMError
    from finpilot.observability.langfuse import score_current_trace
    from finpilot.templating import render_snippet

    extra: list[dict[str, Any]] = []
    problems: list[list[str]] = []
    model = None
    first_pass = False
    out: T | None = None
    rep = GroundingReport()
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            out, res = llm.chat_json(
                prompt_key, variables, schema, agent=agent, extra_messages=list(extra), **kw
            )
            model = res.model or model
        except LLMError:
            out = None
            problems.append(["generation failed"])
            break
        rep = check(texts(out), facts)
        errs = (
            [f"unverified number: {u['text']}" for u in rep.unverified]
            + [f"remove the {e.lower().replace('_', ' ')}" for e in rep.pii]
            + (validate(out) if validate else [])
        )
        problems.append(errs)
        if not errs:
            first_pass = attempt == 0
            break
        extra = [
            {"role": "assistant", "content": out.model_dump_json()},
            {"role": "user", "content": render_snippet("grounding", "retry", problems=errs)},
        ]
        out = None
    fallback_used = out is None
    if out is None:
        out = fallback()
        rep = check(texts(out), facts)
    result = GuardResult(rep, attempts, first_pass, fallback_used, problems, model)
    score_current_trace("grounding_pass", 1.0 if (rep.passed and not fallback_used) else 0.0)
    if any(p for attempt in problems for p in attempt if p.startswith("remove the ")):
        score_current_trace("pii_leak_blocked", 1.0)
    score_current_trace("grounded_number_ratio", rep.ratio)
    return out, result
