"""Mirror eval suites into Langfuse: one Dataset per suite (items = test cases with expected
output) and one dataset run (experiment) per eval run, named `<suite>/<git_sha>/<timestamp>`,
with per-item `correct` scores and a run-level accuracy score. No-ops when Langfuse is off.

The categorizer itself batches merchants into a few LLM calls (traced separately with
`session_id = eval:<run>:<persona>`); the experiment task here replays the stored prediction
for each case, so dataset runs are cheap and exactly match what Postgres recorded.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from eval.suites.base import SuiteResult
from finpilot.config import get_settings
from finpilot.observability.langfuse import client, flush, langfuse_enabled

log = logging.getLogger(__name__)
MAX_ITEMS = 300  # deterministic sample per suite keeps the self-hosted stack light


def _sample(cases: list[Any]) -> list[Any]:
    ranked = sorted(cases, key=lambda c: hashlib.sha256(c.case_id.encode()).hexdigest())
    failing = [c for c in ranked if not c.passed][: MAX_ITEMS // 2]
    passing = [c for c in ranked if c.passed][: MAX_ITEMS - len(failing)]
    return failing + passing


def sync(result: SuiteResult, run_name: str, mode: str) -> str | None:
    if not langfuse_enabled() or not result.cases:
        return None
    lf = client()
    name = f"finpilot-{result.suite}"
    try:
        lf.create_dataset(
            name=name, description=f"FinPilot {result.suite} eval cases (synthetic personas)"
        )
    except Exception:
        log.debug("dataset exists", exc_info=True)
    cases = _sample(result.cases)
    by_item: dict[str, Any] = {}
    for c in cases:
        item_id = "c" + hashlib.sha256(f"{result.suite}:{c.case_id}".encode()).hexdigest()[:30]
        lf.create_dataset_item(
            dataset_name=name,
            id=item_id,
            input=c.input,
            expected_output=c.expected,
            metadata={"case_id": c.case_id},
        )
        by_item[item_id] = c
    dataset = lf.get_dataset(name)
    items = [i for i in dataset.items if i.id in by_item]

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        return by_item[item.id].actual

    def correct(*, input: Any, output: Any, expected_output: Any, **_: Any) -> Any:
        from langfuse import Evaluation

        ok = (output or {}).get("category") == (expected_output or {}).get("category")
        return Evaluation(name="correct", value=1.0 if ok else 0.0)

    def accuracy(*, item_results: list[Any], **_: Any) -> Any:
        from langfuse import Evaluation

        vals = [e.value for r in item_results for e in r.evaluations if e.name == "correct"]
        return Evaluation(name="accuracy", value=sum(vals) / len(vals) if vals else 0.0)

    # Only this run's sampled items: the dataset accumulates items from earlier runs, and replaying
    # all of them would hit items with no prediction in this run.
    res = lf.run_experiment(
        name=f"{result.suite}-{mode}",
        run_name=run_name,
        data=items,
        task=task,
        evaluators=[correct],
        run_evaluators=[accuracy],
        metadata={"mode": mode, "suite": result.suite},
        max_concurrency=10,
    )
    # Link Postgres cases to their Langfuse traces for the Failures explorer.
    by_case: dict[str, str | None] = {}
    for r in res.item_results:
        rid = getattr(r.item, "id", None)
        if getattr(r, "trace_id", None) and rid in by_item:
            by_case[by_item[rid].case_id] = r.trace_id
    for c in result.cases:
        c.langfuse_trace_id = by_case.get(c.case_id)
    flush()
    url = getattr(res, "dataset_run_url", None) or getattr(res, "experiment_url", None)
    return (
        url
        or f"{get_settings().langfuse_host}/project/{get_settings().langfuse_init_project_id}/datasets"
    )
