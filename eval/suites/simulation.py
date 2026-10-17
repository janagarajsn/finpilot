"""Simulation suite.

1. Properties (deterministic, on real persona data in Postgres, ~40 seeded random scenarios per
   persona): a purchase never raises any path's end balance; the expected end-balance delta equals
   the purchase; cancelling a subscription raises the end balance by charge × occurrences in
   every path; same seed → identical result; a composite's expected delta = sum of its parts.
2. Historical backtest: for personas with a real large one-off purchase, start the forecast the
   day before it and compare how often the realized balance stays inside p10–p90 for the
   baseline vs the scenario that includes the purchase (the scenario should explain reality).
3. NL parsing (full mode only, Mesh): eval/datasets/simulator_nl.yml; slot accuracy, intent
   accuracy and clarification correctness (the parser must leave missing slots empty).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from sqlalchemy import text

from eval import loader
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import generate
from eval.synth.personas import PERSONAS
from finpilot.agents.simulator_graph import PROMPT, missing_slots
from finpilot.db.session import user_session
from finpilot.forecasting import inputs as inputs_mod
from finpilot.forecasting import scenarios
from finpilot.forecasting.model import simulate, stable_hash
from finpilot.llm.schemas import ParsedScenario

log = logging.getLogger(__name__)
SUITE = "simulation"
NL_SET = Path(__file__).resolve().parents[1] / "datasets" / "simulator_nl.yml"
N_RANDOM = 40


def _check_properties(
    s: Any, uid: Any, as_of: dt.date, persona: str, rng: np.random.Generator
) -> list[Case]:
    inp, ctx = inputs_mod.build(
        s, uid, as_of=as_of, horizon=90, seed=stable_hash(f"{persona}:{as_of}")
    )
    inp.n_paths = 400
    base = simulate(inp)
    cases: list[Case] = []

    def case(name: str, ok: bool, detail: dict[str, Any]) -> None:
        ok = bool(ok)  # numpy bools would be stored as strings
        cases.append(
            Case(
                case_id=f"{persona}:{name}",
                input={"persona": persona, "property": name.split("#")[0], **detail},
                expected={"holds": True},
                actual={"holds": ok},
                passed=ok,
                score=1.0 if ok else 0.0,
                error_type=None if ok else "property_violated",
            )
        )

    for i in range(N_RANDOM):
        amount = Decimal(str(round(float(rng.uniform(1, 3)) * 10 ** float(rng.uniform(1, 5)), 2)))
        day = int(rng.integers(1, 91))
        sc = scenarios.parse(
            {
                "type": "one_time_purchase",
                "amount": str(amount),
                "date": str(as_of + dt.timedelta(days=day)),
            }
        )
        b = simulate(scenarios.Applier(s, inp, ctx).apply(sc))
        case(
            f"purchase_never_raises#{i}",
            bool((b.end_per_path <= base.end_per_path + 1e-6).all()),
            {"amount": str(amount), "day": day},
        )
        case(
            f"delta_equals_purchase#{i}",
            abs((base.mean[-1] - b.mean[-1]) - float(amount)) < 0.01,
            {"amount": str(amount)},
        )
        if i < 10:
            cut = (
                {
                    "type": "category_cut",
                    "category": inp.streams.keys[0],
                    "pct": float(rng.uniform(5, 60)),
                }
                if inp.streams.keys
                else None
            )
            parts = [sc.model_dump(mode="json")] + ([cut] if cut else [])
            comp = simulate(
                scenarios.Applier(s, inp, ctx).apply(
                    scenarios.parse({"type": "composite", "parts": parts})
                )
            )
            single = [
                simulate(scenarios.Applier(s, inp, ctx).apply(scenarios.parse(p))).mean[-1]
                - base.mean[-1]
                for p in parts
            ]
            case(
                f"composite_sum_of_parts#{i}",
                abs((comp.mean[-1] - base.mean[-1]) - sum(single)) < 0.01,
                {"parts": len(parts)},
            )
    again = simulate(inp)
    case("same_seed_identical", bool(np.array_equal(again.end_per_path, base.end_per_path)), {})
    subs = [
        d
        for d in ctx.detected
        if d["kind"] == "expense" and d["status"] == "active" and not d["dismissed"]
    ]
    for d in subs[:3]:
        removed = [it for it in inp.scheduled if it.ref_id == str(d["id"])]
        if not removed:
            continue
        b = simulate(
            scenarios.Applier(s, inp, ctx).apply(
                scenarios.parse({"type": "cancel_subscription", "subscription_id": str(d["id"])})
            )
        )
        expect = -sum(it.amount for it in removed)
        fixed = all(it.cv == 0 and it.currency is None for it in removed)
        ok = (
            bool(np.allclose(b.end_per_path - base.end_per_path, expect))
            if fixed
            else abs((b.mean[-1] - base.mean[-1]) - expect) < 0.01
        )
        case(
            f"cancel_raises_by_charges#{d['merchant_norm']}",
            ok,
            {"merchant": d["merchant_norm"], "charges": len(removed), "pathwise": fixed},
        )
    return cases


def _historical(ctx: SuiteContext) -> tuple[list[dict[str, Any]], list[Case]]:
    rows, cases = [], []
    for p in PERSONAS:
        if not p.one_off or (ctx.personas and p.name not in ctx.personas):
            continue
        ds = generate(p)
        one = next(a for a in ds.anomalies if a.type == "large_one_off")
        origin = one.date - dt.timedelta(days=1)
        txn = next(t for t in ds.txns if t.id == one.txn_ids[0])
        # the forecaster must not see the purchase: load only what happened up to the origin + the future for scoring
        ld = loader.load(ds, ctx.run_tag, txns=ds.txns)
        loader.apply_truth_categories(ds, ld)
        try:
            with user_session(ld.user_id) as s:
                daily = {
                    r[0]: float(r[1])
                    for r in s.execute(
                        text(
                            "SELECT date, sum(amount_reporting) FROM v_transactions_reporting WHERE user_id = :u GROUP BY date"
                        ),
                        {"u": ld.user_id},
                    )
                }
                inp, fctx = inputs_mod.build(
                    s, ld.user_id, as_of=origin, horizon=60, seed=stable_hash(f"{p.name}:{origin}")
                )
                sc = scenarios.parse(
                    {
                        "type": "one_time_purchase",
                        "amount": str(abs(txn.amount)),
                        "date": str(one.date),
                        "currency": txn.currency,
                    }
                )
                b0, b1 = simulate(inp), simulate(scenarios.Applier(s, inp, fctx).apply(sc))
            actual = inp.start_balance + np.cumsum(
                [daily.get(origin + dt.timedelta(days=k), 0.0) for k in range(1, 61)]
            )
            cov0 = float(((actual >= b0.p10) & (actual <= b0.p90)).mean())
            cov1 = float(((actual >= b1.p10) & (actual <= b1.p90)).mean())
            err0, err1 = (
                float(np.mean(np.abs(b0.p50 - actual))),
                float(np.mean(np.abs(b1.p50 - actual))),
            )
            rows.append(
                {
                    "persona": p.name,
                    "purchase": str(abs(txn.amount)),
                    "currency": txn.currency,
                    "date": one.date.isoformat(),
                    "coverage_baseline": round(cov0, 4),
                    "coverage_scenario": round(cov1, 4),
                    "mae_baseline": round(err0, 2),
                    "mae_scenario": round(err1, 2),
                }
            )
            ok = err1 < err0
            cases.append(
                Case(
                    case_id=f"{p.name}:historical_one_off",
                    input={"persona": p.name, "purchase": str(abs(txn.amount))},
                    expected={"scenario_closer_than_baseline": True},
                    actual={
                        "mae_baseline": round(err0, 2),
                        "mae_scenario": round(err1, 2),
                        "coverage_scenario": round(cov1, 4),
                    },
                    passed=ok,
                    score=round(cov1, 4),
                    error_type=None if ok else "scenario_not_closer",
                )
            )
        finally:
            loader.cleanup(ctx.run_tag)
    return rows, cases


def _nl(ctx: SuiteContext) -> tuple[dict[str, float], list[Case]]:
    spec = yaml.safe_load(NL_SET.read_text(encoding="utf-8"))
    cases: list[Case] = []
    intents = slots = slots_total = clar_ok = clar_total = 0
    for c in spec["cases"]:
        variables = {
            "today": spec["today"],
            "currency": "INR",
            "categories": "Dining Out, Groceries, Shopping, Travel, Entertainment, Fuel",
            "subscriptions": "Netflix, Spotify, Disney Hotstar",
            "question": c["q"],
            "clarifications": [],
        }
        try:
            parsed, _ = ctx.llm.chat_json(
                PROMPT,
                variables,
                ParsedScenario,
                agent="simulator_parser",
                cache_scope="eval:simulator_nl",
                session_id=f"eval:{ctx.run_tag}:simulator_nl",
            )
            got: dict[str, Any] = parsed.model_dump()
            err = None
        except Exception as e:  # schema failure counts as a miss
            got, err = {}, type(e).__name__
        exp = c["expect"]
        ok_fields = {}
        for k, v in exp.items():
            g = got.get(k)
            if k in ("merchant", "category"):
                ok_fields[k] = bool(g) and str(v).lower() in str(g).lower()
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                ok_fields[k] = g is not None and abs(float(g) - float(v)) < 0.01
            else:
                ok_fields[k] = g == v
        intents += bool(ok_fields.get("intent"))
        slots += sum(ok_fields.values())
        slots_total += len(ok_fields)
        clar = None
        if c.get("needs"):
            clar_total += 1
            ms = missing_slots(ParsedScenario.model_validate(got)) if got else []
            want = {"amount": {"amount", "total"}, "pct": {"pct"}, "n": {"n"}}[c["needs"]]
            clar = bool(want & set(ms))
            clar_ok += clar
        passed = all(ok_fields.values()) and (clar is not False)
        cases.append(
            Case(
                case_id=f"nl:{c['id']}",
                input={"question": c["q"]},
                expected={**exp, **({"needs": c["needs"]} if c.get("needs") else {})},
                actual={k: got.get(k) for k in ParsedScenario.model_fields}
                if got
                else {"error": err},
                passed=passed,
                score=sum(ok_fields.values()) / len(ok_fields),
                error_type=None
                if passed
                else (
                    "schema" if err else "wrong_slot" if ok_fields.get("intent") else "wrong_intent"
                ),
            )
        )
    n = len(spec["cases"])
    return {
        "nl_intent_accuracy": intents / n,
        "nl_slot_accuracy": slots / max(slots_total, 1),
        "nl_clarification_accuracy": clar_ok / max(clar_total, 1),
        "nl_exact": sum(c.passed for c in cases) / n,
    }, cases


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    rng = np.random.default_rng(2026)
    personas = [p for p in PERSONAS if not ctx.personas or p.name in ctx.personas][:3]
    for p in personas:
        ds = generate(p)
        ld = loader.load(ds, ctx.run_tag)
        loader.apply_truth_categories(ds, ld)
        try:
            with user_session(ld.user_id) as s:
                res.cases += _check_properties(
                    s, ld.user_id, max(t.date for t in ds.history()), p.name, rng
                )
        finally:
            loader.cleanup(ctx.run_tag)
    props = [c for c in res.cases]
    hist_rows, hist_cases = _historical(ctx)
    res.cases += hist_cases

    def add(metric: str, value: float, unit: str, **dim: Any) -> None:
        res.metrics.append(Metric(metric, float(value), unit, dim))

    add(
        "property_pass_rate",
        sum(c.passed for c in props) / max(len(props), 1),
        "ratio",
        scope="overall",
        primary=True,
    )
    add("n_property_checks", len(props), "count", scope="overall", primary=True)
    by_prop: dict[str, list[bool]] = {}
    for c in props:
        by_prop.setdefault(c.input["property"], []).append(c.passed)
    for k, v in sorted(by_prop.items()):
        add("property_pass_rate", sum(v) / len(v), "ratio", scope="property", property=k)
    if hist_rows:
        add(
            "historical_scenario_coverage",
            float(np.mean([r["coverage_scenario"] for r in hist_rows])),
            "ratio",
            scope="overall",
            primary=True,
        )
        add(
            "historical_baseline_coverage",
            float(np.mean([r["coverage_baseline"] for r in hist_rows])),
            "ratio",
            scope="overall",
            primary=True,
        )
        add(
            "historical_scenario_closer",
            float(np.mean([r["mae_scenario"] < r["mae_baseline"] for r in hist_rows])),
            "ratio",
            scope="overall",
            primary=True,
        )
    nl: dict[str, float] = {}
    if ctx.mode == "full" and ctx.llm is not None:
        nl, nl_cases = _nl(ctx)
        res.cases += nl_cases
        for name, value in nl.items():
            add(name, value, "ratio", scope="overall", primary=True)
    res.details = {
        "properties": {
            k: {"checks": len(v), "passed": int(sum(v))} for k, v in sorted(by_prop.items())
        },
        "historical": hist_rows,
        "nl": nl,
    }
    return res
