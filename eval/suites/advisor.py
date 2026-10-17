"""Advisor suite (BUILD_PROMPT §Evaluation 6).

Per persona (history months, ground-truth categories, as_of = last history day):
  - run the advisor (offline: deterministic templates; full: smart LLM via Mesh + guard)
  - per recommendation: cites ≥ 1 finding of its candidate; names a specific entity; grounded
    (every number in its text is a computed fact)
  - grounding first-attempt pass rate and fallback rate (full)
  - LLM-as-judge via Mesh (prompts/eval_judge/advice.v1.md, committed rubric; full)
  - dismiss the top recommendation and re-run: it must not come back (memory of dismissals)
Across personas: share of personas whose top recommendation differs from every other persona's.
Guard robustness (deterministic, both modes): corrupt one amount in each real recommendation
text (×1.37, a fabricated sentence, or the wrong currency symbol) → the guard must flag it;
clean texts must pass.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from typing import Any

from eval import loader
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import generate
from eval.synth.personas import PERSONAS
from finpilot.agents.advisor import Advisor, decide, get_trail, mentions_entity
from finpilot.db.session import user_session
from finpilot.llm.schemas import JudgeResponse
from finpilot.trail.grounding import check, extract

log = logging.getLogger(__name__)
SUITE = "advisor"


def _perturb(text: str) -> list[tuple[str, str]]:
    """Corrupted copies of `text`: (kind, text). Only texts with an amount can be scaled/swapped."""
    out = [("fabricated_sentence", text + " That is ₹73,219 extra a year.")]
    money = [m for m in extract(text) if m.kind == "money" and m.value and abs(m.value) >= 10]
    if money:
        m = money[0]
        assert m.value is not None
        scaled = re.sub(
            r"\d[\d,]*(?:\.\d+)?", f"{abs(m.value) * 137 / 100:,.0f}", m.text.strip(), count=1
        )
        out.append(("scaled_amount", text.replace(m.text.strip(), scaled, 1)))
        if "₹" in m.text:
            out.append(
                ("currency_swap", text.replace(m.text.strip(), m.text.strip().replace("₹", "$"), 1))
            )
    return out


def _judge(
    ctx: SuiteContext,
    persona: str,
    recs: list[dict[str, Any]],
    trails: list[dict[str, Any]],
    currency: str,
) -> list[dict[str, Any]]:
    findings = {
        f["id"]: {"title": f["title"], "detail": f["body"]} for t in trails for f in t["findings"]
    }
    facts = {r["id"]: t["candidate_json"].get("facts") for r, t in zip(recs, trails, strict=True)}
    items = [
        {"recommendation_id": r["id"], "title": r["title"], "action": r["action"], "why": r["why"]}
        for r in recs
    ]
    try:
        out, _ = ctx.llm.chat_json(
            "eval_judge/advice",
            {
                "currency": currency,
                "findings": json.dumps(list(findings.values()), ensure_ascii=False, indent=1),
                "facts": json.dumps(facts, default=str, ensure_ascii=False, indent=1),
                "recommendations": json.dumps(items, ensure_ascii=False, indent=1),
            },
            JudgeResponse,
            agent="eval_judge",
            cache_scope=f"eval:{persona}",
            session_id=f"eval:{ctx.run_tag}:judge:{persona}",
        )
        return [s.model_dump() for s in out.scores]
    except Exception:
        log.warning("judge failed for %s", persona, exc_info=True)
        return []


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas]
    llm = ctx.llm if ctx.mode == "full" else None
    recs_all: list[dict[str, Any]] = []
    first_pass: list[bool] = []
    fallback: list[bool] = []
    not_repeated: list[bool] = []
    judge_scores: list[dict[str, Any]] = []
    tops: dict[str, str] = {}
    pert_detected: list[bool] = []
    clean_ok: list[bool] = []
    per_persona: list[dict[str, Any]] = []
    for p in personas:
        ds = generate(p)
        ld = loader.load(ds, ctx.run_tag, with_opening_balance=True)
        loader.apply_truth_categories(ds, ld)
        as_of = max(t.date for t in ds.history())
        try:
            with user_session(ld.user_id) as s:
                adv = Advisor(llm)
                a, findings, cands = adv.prepare(s, ld.user_id, as_of=as_of)
                by_key = {c.key: c for c in cands}
                run_ = adv.run(s, ld.user_id, as_of=as_of)
                trails = [get_trail(s, ld.user_id, r["id"]) for r in run_.recommendations]
                if llm is not None:
                    first_pass.append(bool(run_.guard.get("first_attempt_pass")))
                    fallback.append(bool(run_.guard.get("fallback_used")))
                scoped = adv.scoped_facts(a, findings, cands)
                for r, t in zip(run_.recommendations, trails, strict=True):
                    c = by_key.get(r["candidate_key"])
                    textv = f"{r['title']} {r['action']} {r['why']}"
                    cited = bool(c and set(r["cited_finding_ids"]) & set(c.finding_ids))
                    specific = bool(c and mentions_entity(textv, c))
                    grounded = (
                        c is not None
                        and check([r["title"], r["action"], r["why"]], scoped[c.key]).passed
                    )
                    recs_all.append(
                        {
                            "persona": p.name,
                            "cited": cited,
                            "specific": specific,
                            "grounded": grounded,
                        }
                    )
                    res.cases.append(
                        Case(
                            case_id=f"{p.name}:rec{r['priority']}",
                            input={
                                "persona": p.name,
                                "candidate": r["candidate_key"],
                                "trigger": r["trigger_kind"],
                            },
                            expected={"cited": True, "specific": True, "grounded": True},
                            actual={
                                "title": r["title"],
                                "action": r["action"],
                                "why": r["why"],
                                "cited": cited,
                                "specific": specific,
                                "grounded": grounded,
                                "generated_by": r["generated_by"],
                                "unverified": t["grounding_report_json"]
                                .get("this_recommendation", {})
                                .get("unverified"),
                            },
                            passed=cited and specific and grounded,
                            score=(cited + specific + grounded) / 3,
                            error_type=None
                            if cited and specific and grounded
                            else (
                                "ungrounded"
                                if not grounded
                                else "generic"
                                if not specific
                                else "uncited"
                            ),
                            langfuse_trace_id=t.get("langfuse_trace_id"),
                        )
                    )
                    clean_ok.append(grounded)
                    for kind, bad in _perturb(r["why"]):
                        caught = c is None or not check([bad], scoped[c.key]).passed
                        pert_detected.append(caught)
                        res.cases.append(
                            Case(
                                case_id=f"{p.name}:rec{r['priority']}:{kind}",
                                input={"persona": p.name, "perturbation": kind, "text": bad},
                                expected={"flagged": True},
                                actual={"flagged": caught},
                                passed=caught,
                                score=float(caught),
                                error_type=None if caught else "guard_missed",
                            )
                        )
                if run_.recommendations:
                    top = run_.recommendations[0]
                    tops[p.name] = top["candidate_key"]
                    if llm is not None:
                        for sc in _judge(ctx, p.name, run_.recommendations, trails, a.currency):
                            judge_scores.append({"persona": p.name, **sc})
                    decide(s, ld.user_id, top["id"], "dismiss", "Not relevant for me right now")
                    again = Advisor(llm).run(s, ld.user_id, as_of=as_of)
                    not_repeated.append(
                        top["candidate_key"]
                        not in {r["candidate_key"] for r in again.recommendations}
                    )
            per_persona.append(
                {
                    "persona": p.name,
                    "recommendations": len(run_.recommendations),
                    "candidates": len(cands),
                    "top": tops.get(p.name),
                    "generated_by": run_.generated_by,
                }
            )
        finally:
            loader.cleanup(ctx.run_tag)

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    n = max(len(recs_all), 1)
    add(
        "pct_with_finding",
        sum(r["cited"] for r in recs_all) / n,
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "pct_specific",
        sum(r["specific"] for r in recs_all) / n,
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "pct_grounded",
        sum(r["grounded"] for r in recs_all) / n,
        "ratio",
        scope="overall",
        primary=True,
    )
    keys = list(tops.values())
    distinct = sum(1 for k in keys if keys.count(k) == 1) / max(len(keys), 1)
    add("personas_distinct_top", distinct, "ratio", scope="overall", primary=True)
    add(
        "dismissed_not_repeated",
        sum(not_repeated) / max(len(not_repeated), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "guard_detection_rate",
        sum(pert_detected) / max(len(pert_detected), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add(
        "guard_clean_pass_rate",
        sum(clean_ok) / max(len(clean_ok), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add("n_recommendations", len(recs_all), "count", scope="overall", primary=True)
    if first_pass:
        add(
            "grounding_first_attempt_pass_rate",
            sum(first_pass) / len(first_pass),
            "ratio",
            scope="overall",
            primary=True,
        )
        add("fallback_rate", sum(fallback) / len(fallback), "ratio", scope="overall", primary=True)
    if judge_scores:
        for k in ("overall", "specificity", "actionability", "grounded", "relevance"):
            add(
                f"judge_{k}",
                statistics.mean(s[k] for s in judge_scores) / 5,
                "ratio",
                scope="overall",
                primary=k == "overall",
            )
    res.details = {
        "per_persona": per_persona,
        "tops": tops,
        "judge": judge_scores,
        "perturbations": {"checked": len(pert_detected), "detected": sum(pert_detected)},
        "facts_note": "LLM sees candidates + facts only; numbers verified against the same facts",
    }
    return res
