"""Evaluation results (readable by every signed-in user) and admin-only run trigger."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text

from finpilot.redis_client import get_redis
from finpilot.tenancy import Principal, get_principal, require_admin

router = APIRouter(prefix="/eval", tags=["eval"])


def _engine():
    from eval.store import engine

    return engine()


def _rows(sql: str, **params: Any) -> list[dict[str, Any]]:
    with _engine().connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


@router.get("/runs")
def runs(
    limit: int = Query(default=30, ge=1, le=200), _: Principal = Depends(get_principal)
) -> list[dict]:
    return _rows(
        """SELECT id, started_at, finished_at, git_sha, trigger, mode, status,
                           summary_json->'headline' AS headline
                    FROM eval_runs ORDER BY started_at DESC LIMIT :l""",
        l=limit,
    )


def run_detail(run_id: uuid.UUID) -> dict[str, Any]:
    run = _rows("SELECT * FROM eval_runs WHERE id = :r", r=run_id)
    if not run:
        raise HTTPException(404, "Eval run not found")
    out = run[0]
    out["metrics"] = _rows(
        """SELECT suite, metric, value, unit, dimension_json AS dimension FROM eval_metrics
                              WHERE run_id = :r ORDER BY suite, metric""",
        r=run_id,
    )
    out["insights"] = _rows(
        """SELECT severity, title, body, evidence_json AS evidence, generated_by FROM eval_insights
                               WHERE run_id = :r ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id""",
        r=run_id,
    )
    out["case_counts"] = _rows(
        """SELECT suite, count(*) AS total, count(*) FILTER (WHERE NOT passed) AS failed
                                  FROM eval_cases WHERE run_id = :r GROUP BY suite""",
        r=run_id,
    )
    prev = _rows(
        """SELECT id, summary_json->'headline' AS headline FROM eval_runs WHERE mode = :m AND id <> :r
                    AND status IN ('passed','failed') AND started_at < :t ORDER BY started_at DESC LIMIT 1""",
        m=out["mode"],
        r=run_id,
        t=out["started_at"],
    )
    out["previous"] = prev[0] if prev else None
    return out


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, _: Principal = Depends(get_principal)) -> dict:
    return run_detail(run_id)


@router.get("/runs/{run_id}/cases")
def cases(
    run_id: uuid.UUID,
    suite: str = "categorization",
    passed: bool | None = False,
    error_type: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(get_principal),
) -> dict:
    where = ["run_id = :r", "suite = :s"]
    params: dict[str, Any] = {"r": run_id, "s": suite, "l": limit, "o": offset}
    if passed is not None:
        where.append("passed = :p")
        params["p"] = passed
    if error_type:
        where.append("error_type = :e")
        params["e"] = error_type
    if q:
        where.append(
            "(case_id ILIKE :q OR input_json::text ILIKE :q OR expected_json::text ILIKE :q)"
        )
        params["q"] = f"%{q}%"
    cond = " AND ".join(where)  # fixed clause strings only; values are bound
    total = _rows(f"SELECT count(*) AS n FROM eval_cases WHERE {cond}", **params)[0]["n"]  # noqa: S608
    page_sql = (
        "SELECT case_id, input_json AS input, expected_json AS expected, actual_json AS actual, passed, "  # noqa: S608
        f"score, error_type, langfuse_trace_id FROM eval_cases WHERE {cond} ORDER BY case_id LIMIT :l OFFSET :o"
    )
    items = _rows(page_sql, **params)
    return {"total": total, "items": items}


@router.get("/compare")
def compare(a: uuid.UUID, b: uuid.UUID, _: Principal = Depends(get_principal)) -> dict:
    rows = _rows(
        """SELECT run_id, suite, metric, value, dimension_json->>'variant' AS variant FROM eval_metrics
                    WHERE run_id IN (:a, :b) AND dimension_json->>'scope' = 'overall'""",
        a=a,
        b=b,
    )
    table: dict[str, dict[str, float]] = {}
    for r in rows:
        key = f"{r['suite']}.{r['metric']}[{r['variant']}]"
        table.setdefault(key, {})["a" if r["run_id"] == a else "b"] = r["value"]
    from eval.insights import LOWER_IS_BETTER

    return {
        "a": str(a),
        "b": str(b),
        "metrics": [
            {
                "metric": k,
                "lower_is_better": k.split(".", 1)[-1].split("[")[0] in LOWER_IS_BETTER,
                **v,
                "delta": (v.get("b", 0) - v.get("a", 0)) if "a" in v and "b" in v else None,
            }
            for k, v in sorted(table.items())
        ],
    }


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
def trigger(
    mode: Literal["offline", "full"] = "offline", _: Principal = Depends(require_admin)
) -> dict:
    if get_redis().exists("lock:evalrun"):
        raise HTTPException(409, "An evaluation is already running")
    from finpilot.tasks.eval_tasks import run_eval

    task = run_eval.delay(mode, "manual")
    return {"task_id": task.id, "mode": mode}
