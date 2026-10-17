"""Guardrails suite: PII masking (what leaves FinPilot), PII leak check (what users see), advice
scope (no investment / tax / legal directives) and structural prompt-injection containment.

Deterministic, no network, identical in offline and full mode. Dataset: eval/datasets/guardrails.yml
(written with the implementation: the gates guard against regressions).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from finpilot.llm.prompt_registry import get_registry
from finpilot.privacy import pii
from finpilot.trail.grounding import FactSet, check
from finpilot.trail.scope import violations

SUITE = "guardrails"
DATA = Path(__file__).resolve().parents[1] / "datasets" / "guardrails.yml"


def _inside_data(rendered: str, needle: str) -> bool:
    """Every occurrence of `needle` sits between a <data> and its </data>."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"<data>.*?</data>", rendered, re.S)]
    hits = [m.start() for m in re.finditer(re.escape(needle), rendered)]
    return bool(hits) and all(any(a <= h < b for a, b in spans) for h in hits)


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    d: dict[str, Any] = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    metrics: dict[str, float] = {}
    pii.analyzer()  # warm up outside the timings
    lat: list[float] = []

    def case(cid: str, inp: Any, exp: Any, act: Any, ok: bool, err: str) -> bool:
        res.cases.append(Case(cid, {"text": inp}, exp, act, ok, error_type=None if ok else err))
        return ok

    ok = 0
    for p in d["pii_positive"]:
        t0 = time.perf_counter()
        out, counts = pii.mask(p["text"])
        lat.append((time.perf_counter() - t0) * 1000)
        ok += case(
            f"mask:{p['entity']}:{p['text'][:40]}",
            p["text"],
            {"masked": p["entity"]},
            {"out": out, "entities": sorted(counts)},
            p["secret"] not in out,
            "pii_not_masked",
        )
    metrics["pii_mask_recall"] = ok / len(d["pii_positive"])

    changed = 0
    for t in d["pii_negative"]:
        t0 = time.perf_counter()
        out, _ = pii.mask(t)
        lat.append((time.perf_counter() - t0) * 1000)
        changed += not case(
            f"keep:{t[:40]}", t, {"unchanged": True}, {"out": out}, out == t, "figure_masked"
        )
    metrics["pii_false_mask_rate"] = changed / len(d["pii_negative"])
    metrics["pii_mask_p95_ms"] = float(np.percentile(lat, 95))

    facts = FactSet()
    leak_ok = sum(
        case(
            f"leak:{t[:40]}",
            t,
            {"blocked": True},
            {"pii": check([t], facts).pii},
            bool(check([t], facts).pii),
            "leak_not_blocked",
        )
        for t in d["answers_leak"]
    )
    clean_ok = sum(
        case(
            f"clean_answer:{t[:40]}",
            t,
            {"pii": []},
            {"pii": check([t], facts).pii},
            not check([t], facts).pii,
            "clean_answer_blocked",
        )
        for t in d["answers_clean"]
    )
    metrics["leak_detection_rate"] = leak_ok / len(d["answers_leak"])
    metrics["leak_clean_pass_rate"] = clean_ok / len(d["answers_clean"])

    hit = sum(
        case(
            f"directive:{x['kind']}:{x['text'][:40]}",
            x["text"],
            {"kind": x["kind"]},
            {"kinds": [k for k, _ in violations(x["text"])]},
            [k for k, _ in violations(x["text"])] == [x["kind"]],
            "directive_missed",
        )
        for x in d["directives"]
    )
    clean = sum(
        case(
            f"lever:{t[:40]}",
            t,
            {"kinds": []},
            {"kinds": [k for k, _ in violations(t)]},
            not violations(t),
            "lever_flagged",
        )
        for t in d["directives_clean"]
    )
    metrics["directive_detection_rate"] = hit / len(d["directives"])
    metrics["directive_clean_pass_rate"] = clean / len(d["directives_clean"])

    inj = d["injection"]
    reg = get_registry()
    rendered = {
        "categorizer/classify": reg.get("categorizer/classify").render(
            taxonomy=[],
            custom_categories=[],
            examples=[],
            currency_note="",
            merchants=[
                {
                    "id": "m1",
                    "direction": "out",
                    "amount": "₹500",
                    "count": 1,
                    "samples": [inj],
                    "hint": None,
                }
            ],
        ),
        "intent_router/classify": reg.get("intent_router/classify").render(
            intents="-", question=inj
        ),
    }
    contained = sum(
        case(
            f"injection:{k}",
            inj,
            {"inside_data": True},
            {"inside_data": _inside_data(" ".join(str(m.get("content")) for m in msgs), inj)},
            _inside_data(" ".join(str(m.get("content")) for m in msgs), inj),
            "injection_outside_data",
        )
        for k, msgs in rendered.items()
    )
    metrics["injection_contained"] = contained / len(rendered)

    for k, v in metrics.items():
        res.metrics.append(
            Metric(
                k,
                float(v),
                "ms" if k.endswith("_ms") else "ratio",
                {"scope": "overall", "primary": True},
            )
        )
    res.details = {
        "pii_cases": len(d["pii_positive"]) + len(d["pii_negative"]),
        "answers": len(d["answers_leak"]) + len(d["answers_clean"]),
        "directives": len(d["directives"]) + len(d["directives_clean"]),
        "entities": sorted(pii.config()["placeholders"]),
    }
    return res
