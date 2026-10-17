"""Persists eval runs, metrics (long format), cases and insights in Postgres (admin role)."""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from eval.suites.base import SuiteResult
from finpilot.config import get_settings


@lru_cache
def engine() -> Engine:
    return create_engine(get_settings().database_admin_url, pool_pre_ping=True)


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


def create_run(
    trigger: str, mode: str, git_sha: str | None, config: dict, models: dict, prompts: dict
) -> uuid.UUID:
    with engine().begin() as c:
        return c.execute(
            text("""INSERT INTO eval_runs (trigger, mode, git_sha, config_json, models_json, prompt_versions_json)
                                 VALUES (:t, :m, :g, CAST(:c AS jsonb), CAST(:mo AS jsonb), CAST(:p AS jsonb)) RETURNING id"""),
            {
                "t": trigger,
                "m": mode,
                "g": git_sha,
                "c": _j(config),
                "mo": _j(models),
                "p": _j(prompts),
            },
        ).scalar_one()


def save_suite(run_id: uuid.UUID, r: SuiteResult) -> None:
    with engine().begin() as c:
        if r.metrics:
            c.execute(
                text("""INSERT INTO eval_metrics (run_id, suite, metric, value, unit, dimension_json)
                              VALUES (:r, :s, :m, :v, :u, CAST(:d AS jsonb))"""),
                [
                    {
                        "r": run_id,
                        "s": r.suite,
                        "m": m.metric,
                        "v": m.value,
                        "u": m.unit,
                        "d": _j(m.dimension),
                    }
                    for m in r.metrics
                ],
            )
        if r.cases:
            c.execute(
                text("""INSERT INTO eval_cases (run_id, suite, case_id, input_json, expected_json, actual_json,
                                passed, score, error_type, langfuse_trace_id)
                              VALUES (:r, :s, :id, CAST(:i AS jsonb), CAST(:e AS jsonb), CAST(:a AS jsonb), :p, :sc,
                                :err, :lt)"""),
                [
                    {
                        "r": run_id,
                        "s": r.suite,
                        "id": k.case_id,
                        "i": _j(k.input),
                        "e": _j(k.expected),
                        "a": _j(k.actual),
                        "p": k.passed,
                        "sc": k.score,
                        "err": k.error_type,
                        "lt": k.langfuse_trace_id,
                    }
                    for k in r.cases
                ],
            )


def save_insights(run_id: uuid.UUID, insights: list[dict[str, Any]]) -> None:
    if not insights:
        return
    with engine().begin() as c:
        c.execute(
            text("""INSERT INTO eval_insights (run_id, severity, title, body, evidence_json, generated_by)
                          VALUES (:r, :s, :t, :b, CAST(:e AS jsonb), :g)"""),
            [
                {
                    "r": run_id,
                    "s": i["severity"],
                    "t": i["title"],
                    "b": i["body"],
                    "e": _j(i.get("evidence", {})),
                    "g": i.get("generated_by", "rules"),
                }
                for i in insights
            ],
        )


def finish_run(run_id: uuid.UUID, status: str, summary: dict, langfuse_urls: dict) -> None:
    with engine().begin() as c:
        c.execute(
            text("""UPDATE eval_runs SET finished_at = now(), status = :s, summary_json = CAST(:sum AS jsonb),
                          langfuse_run_urls_json = CAST(:lf AS jsonb) WHERE id = :r"""),
            {"s": status, "sum": _j(summary), "lf": _j(langfuse_urls), "r": run_id},
        )


def previous_run(mode: str, before: uuid.UUID) -> uuid.UUID | None:
    """Latest earlier run of the same mode over the same suites and personas (comparable)."""
    with engine().connect() as c:
        return c.execute(
            text("""SELECT p.id FROM eval_runs p, eval_runs cur
                    WHERE cur.id = :r AND p.mode = :m AND p.status IN ('passed','failed') AND p.id <> :r
                      AND p.started_at < cur.started_at
                      AND p.config_json->'suites' = cur.config_json->'suites'
                      AND p.config_json->'personas' IS NOT DISTINCT FROM cur.config_json->'personas'
                    ORDER BY p.started_at DESC LIMIT 1"""),
            {"m": mode, "r": before},
        ).scalar()


def overall_metrics(run_id: uuid.UUID) -> dict[str, float]:
    with engine().connect() as c:
        rows = c.execute(
            text("""SELECT suite, metric, value FROM eval_metrics WHERE run_id = :r
                                 AND dimension_json->>'scope' = 'overall' AND (dimension_json->>'primary')::boolean"""),
            {"r": run_id},
        )
        return {f"{r.suite}.{r.metric}": r.value for r in rows}
