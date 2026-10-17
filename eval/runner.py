"""Eval runner: `python -m eval.runner --mode offline|full [--gate] [--trigger manual|ci|scheduled]`.

Runs suites against fresh synthetic users, stores everything in Postgres (eval_* tables),
mirrors cases to Langfuse datasets, generates deterministic insights, applies the gates in
config/thresholds.yml (exit code 1 on violation with --gate) and prints a summary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import secrets
import subprocess
import sys
import uuid
from typing import Any

from eval import insights as insights_mod
from eval import langfuse_sync, loader, store
from eval.suites import (
    advisor,
    anomalies,
    cache,
    categorization,
    forecast,
    fx,
    guardrails,
    memory,
    recurring,
    simulation,
)
from eval.suites.base import SuiteContext, SuiteResult
from finpilot.categories.rules import thresholds
from finpilot.config import get_settings
from finpilot.llm.client import get_llm
from finpilot.llm.prompt_registry import get_registry

log = logging.getLogger("eval")
SUITES = {
    "categorization": categorization.run,
    "anomalies": anomalies.run,
    "recurring": recurring.run,
    "forecast": forecast.run,
    "simulation": simulation.run,
    "advisor": advisor.run,
    "cache": cache.run,
    "memory": memory.run,
    "fx": fx.run,
    "guardrails": guardrails.run,
}


def git_sha() -> str | None:
    if os.getenv("GIT_SHA"):
        return os.getenv("GIT_SHA")
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            or None
        )
    except Exception:
        return None


def check_gates(mode: str, headline: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for metric, rule in (thresholds().get("eval_gates", {}).get(mode) or {}).items():
        if metric not in headline:
            continue
        v = headline[metric]
        ok = ("min" not in rule or v >= rule["min"]) and ("max" not in rule or v <= rule["max"])
        out.append({"metric": metric, "value": v, "rule": rule, "passed": ok})
    return out


def run(
    mode: str,
    trigger: str = "manual",
    suites: list[str] | None = None,
    personas: list[str] | None = None,
    gate: bool = False,
    langfuse: bool = True,
) -> tuple[uuid.UUID, int]:
    s = get_settings()
    llm = None
    if mode == "full":
        llm = get_llm()
        if not llm.available:
            raise SystemExit("full mode needs MESH_API_KEY (use --mode offline otherwise)")
    from finpilot.categories.seed import sync_system_categories

    sync_system_categories()  # taxonomy must be current (fresh CI databases have none)
    sha = git_sha()
    prompts = {p.key: p.ref for p in get_registry().all()}
    run_id = store.create_run(
        trigger,
        mode,
        sha,
        {"suites": suites or list(SUITES), "personas": personas},
        {"fast": s.mesh_model_fast, "smart": s.mesh_model_smart, "embed": s.mesh_model_embed},
        prompts,
    )
    tag = f"r{secrets.token_hex(4)}"
    ctx = SuiteContext(mode=mode, run_tag=tag, personas=personas, llm=llm)
    results: list[SuiteResult] = []
    urls: dict[str, str] = {}
    status = "error"
    try:
        for name in suites or list(SUITES):
            log.info("running suite %s (%s)", name, mode)
            r = SUITES[name](ctx)
            if langfuse:
                try:
                    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
                    url = langfuse_sync.sync(r, f"{name}/{sha or 'nogit'}/{stamp}", mode)
                    if url:
                        urls[name] = url
                except Exception:
                    log.warning("Langfuse sync failed for %s", name, exc_info=True)
            store.save_suite(run_id, r)
            results.append(r)
        headline = store.overall_metrics(run_id)
        gates = check_gates(mode, headline)
        prev = store.previous_run(mode, run_id)
        ins = insights_mod.generate(
            results, gates, headline, store.overall_metrics(prev) if prev else None
        )
        store.save_insights(run_id, ins)
        failed = [g for g in gates if not g["passed"]]
        status = "failed" if failed else "passed"
        summary = {
            "headline": headline,
            "gates": gates,
            "details": {r.suite: r.details for r in results},
            "previous_run": str(prev) if prev else None,
        }
        store.finish_run(run_id, status, summary, urls)
        _print(run_id, mode, headline, gates, ins)
        return run_id, 1 if (gate and failed) else 0
    except BaseException:
        store.finish_run(run_id, "error", {}, urls)
        raise
    finally:
        loader.cleanup(tag)


def _print(
    run_id: uuid.UUID,
    mode: str,
    headline: dict[str, float],
    gates: list[dict[str, Any]],
    ins: list[dict[str, Any]],
) -> None:
    print(f"\nEval run {run_id} ({mode})")
    for k, v in sorted(headline.items()):
        print(f"  {k:48} {v:8.4f}")
    for g in gates:
        print(
            f"  gate {g['metric']:43} {'PASS' if g['passed'] else 'FAIL'}  {g['value']:.4f} {g['rule']}"
        )
    for i in ins[:8]:
        print(f"  [{i['severity']}] {i['title']}: {i['body']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["offline", "full"], default="offline")
    ap.add_argument("--trigger", choices=["manual", "ci", "scheduled"], default="manual")
    ap.add_argument("--suite", action="append", choices=list(SUITES))
    ap.add_argument("--persona", action="append")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--no-langfuse", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _, code = run(a.mode, a.trigger, a.suite, a.persona, a.gate, not a.no_langfuse)
    return code


if __name__ == "__main__":
    sys.exit(main())
