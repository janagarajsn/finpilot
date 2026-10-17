"""Advisor agent: deterministic candidates → smart LLM picks and words the top 3 → validator +
grounding guard (retry ×2) → template fallback → `recommendations` + `decision_trail`.

The LLM sees opaque ids (c1…, f1…), entity names and computed facts only. A recommendation is
accepted when it refers to a known candidate, cites ≥ 1 of that candidate's findings, names one of
its entities (merchant / category / goal / purchase) and every number in it is a fact.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.advice import candidates as cand_mod
from finpilot.advice.candidates import AdviceInputs, Candidate
from finpilot.agents import goal_tracker
from finpilot.agents.analyzer import Analyzer, get_report, upsert_findings
from finpilot.forecasting import inputs as inputs_mod
from finpilot.forecasting.model import simulate
from finpilot.forecasting.service import get_forecast, run_forecast
from finpilot.forecasting.simulator import outcome
from finpilot.insights.models import Finding
from finpilot.llm.client import LLMClient
from finpilot.llm.prompt_registry import get_registry
from finpilot.llm.schemas import AdviceItem, AdviceResponse
from finpilot.money import get_currency_registry
from finpilot.observability.langfuse import agent_span
from finpilot.templating import render_snippet
from finpilot.trail.grounding import FactSet, GuardResult, check, guarded_json
from finpilot.trail.scope import violations

log = logging.getLogger(__name__)
PROMPT = "advisor/recommend"
MAX_CANDIDATES = 8
MAX_ITEMS = 3
ADVISOR_KINDS = ("forecast", "goal")


@dataclass
class AdvisorRun:
    run_id: str
    generated_by: str  # llm | template
    recommendations: list[dict[str, Any]]
    guard: dict[str, Any]
    n_candidates: int
    duration_ms: int
    trace_id: str | None


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, indent=1)


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3 and not t.isdigit()]


def mentions_entity(text_: str, c: Candidate) -> bool:
    low = text_.lower()
    for k, v in c.entities.items():
        if k in ("date", "dates", "month") or not v:
            continue
        if any(t in low for t in _tokens(str(v))):
            return True
    return False


class Advisor:
    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm

    # ------------------------------------------------------------------ inputs
    def prepare(
        self, s: Session, user_id: uuid.UUID, fx: Any = None, as_of: dt.date | None = None
    ) -> tuple[AdviceInputs, list[Finding], list[Candidate]]:
        """`as_of` None = the user's today (snapshots reused); a date = evals / backtests."""
        if as_of is None:
            report = get_report(s, user_id)
            fc = get_forecast(s, user_id, fx=fx)
        else:
            report = Analyzer().run(s, user_id, as_of=as_of)
            fc = run_forecast(s, user_id, as_of=as_of, fx=fx)
        inp, ctx = inputs_mod.build(
            s, user_id, as_of=fc.as_of, horizon=fc.horizon, fx=fx, refresh_detection=False
        )
        base = simulate(inp)
        spend = -sum(float(st.monthly) for st in fc.streams if st.monthly < 0)
        spend += -sum(float(x.amount) for x in fc.scheduled if x.amount < 0) / fc.horizon * 30.44
        a = AdviceInputs(
            s,
            user_id,
            fc.currency,
            report,
            fc,
            inp,
            ctx,
            base,
            outcome(base, inp.buffer),
            spend,
            cand_mod.dismissed_memory(s, user_id),
        )
        own = cand_mod.advisor_findings(a, cand_mod.recent_risky_scenarios(s, user_id))
        own += goal_tracker.ahead_findings(fc)
        own = upsert_findings(s, user_id, own, fc.as_of, ADVISOR_KINDS)
        findings = list(report.findings) + own
        return a, findings, cand_mod.build(a, findings)

    # ------------------------------------------------------------------ run
    def run(
        self,
        s: Session,
        user_id: uuid.UUID,
        fx: Any = None,
        persist: bool = True,
        as_of: dt.date | None = None,
        run_id: str | None = None,
    ) -> AdvisorRun:
        """`run_id`: the coordinator's pipeline run (recommendations link to it); new otherwise."""
        t0 = time.perf_counter()
        run_id = run_id or str(uuid.uuid4())
        with agent_span("advisor", user_id=user_id, session_id=f"advisor:{run_id}") as trace_id:
            a, findings, cands = self.prepare(s, user_id, fx, as_of)
            cands = cands[:MAX_CANDIDATES]
            items, guard, by_id, generated_by = self._choose(a, findings, cands, user_id)
            scoped = self.scoped_facts(a, findings, cands)
            rows = (
                self._persist(s, a, run_id, items, by_id, guard, generated_by, trace_id, scoped)
                if persist
                else []
            )
        return AdvisorRun(
            run_id,
            generated_by,
            rows if persist else [i.model_dump() for i in items],
            guard.as_dict() if guard else {},
            len(cands),
            int((time.perf_counter() - t0) * 1000),
            trace_id,
        )

    def scoped_facts(
        self, a: AdviceInputs, findings: list[Finding], cands: list[Candidate]
    ) -> dict[str, FactSet]:
        """Per candidate: its own facts + the findings it cites + forecast basics."""
        return {c.key: self._facts(a, findings, [c]) for c in cands}

    def _facts(self, a: AdviceInputs, findings: list[Finding], cands: list[Candidate]) -> FactSet:
        fs = FactSet()
        for c in cands:
            fs.add_obj(c.facts, a.currency)
            for v in c.entities.values():
                fs.add_literal(v)
            fs.add_money(c.est_monthly_impact, a.currency).add_money(c.est_total_impact, a.currency)
        cited = {fid for c in cands for fid in c.finding_ids}
        for f in findings:
            if f.id in cited:
                fs.add_obj(
                    {
                        "currency": f.currency or a.currency,
                        "baseline": f.baseline,
                        "observed": f.observed,
                        "threshold": f.threshold,
                    },
                    a.currency,
                )
                fs.add_obj(f.data, f.data.get("currency") or a.currency)
                fs.add_date(f.period_start).add_date(f.period_end)
        fc = a.forecast
        fs.add_money(fc.buffer, a.currency).add_money(fc.start_balance, a.currency).add_date(
            fc.as_of
        )
        for n in (
            10,
            50,
            90,
            fc.horizon,
            30,
            60,
        ):  # percentiles and horizons used in plain language
            fs.add_number(n)
        return fs

    def _choose(
        self, a: AdviceInputs, findings: list[Finding], cands: list[Candidate], user_id: uuid.UUID
    ) -> tuple[list[AdviceItem], GuardResult | None, dict[str, Candidate], str]:
        by_id = {f"c{i + 1}": c for i, c in enumerate(cands)}
        fid_of: dict[str, str] = {}
        for c in cands:
            for fid in c.finding_ids:
                fid_of.setdefault(fid, f"f{len(fid_of) + 1}")
        real_of = {v: k for k, v in fid_of.items()}
        want = min(MAX_ITEMS, len(cands))
        if want == 0:
            return [], None, by_id, "template"
        f_index = {f.id: f for f in findings}

        def fallback() -> AdviceResponse:
            return AdviceResponse(
                recommendations=[
                    self._template(cid, c, a, fid_of) for cid, c in list(by_id.items())[:want]
                ]
            )

        def validate(r: AdviceResponse) -> list[str]:
            errs = []
            ids = [x.recommendation_id for x in r.recommendations]
            if len(ids) != want or len(set(ids)) != len(ids):
                errs.append(f"choose exactly {want} different candidates")
            for x in r.recommendations:
                c = by_id.get(x.recommendation_id)
                if c is None:
                    errs.append(f"{x.recommendation_id} is not a candidate id")
                    continue
                cited = [real_of.get(f) for f in x.cited_finding_ids]
                if not any(f in c.finding_ids for f in cited if f):
                    errs.append(
                        f"{x.recommendation_id}: cite at least one of its finding ids ({', '.join(fid_of[f] for f in c.finding_ids)})"
                    )
                errs += [
                    f"{x.recommendation_id}: {msg}"
                    for _, msg in violations(f"{x.title} {x.action} {x.why}")
                ]
                if not mentions_entity(f"{x.title} {x.action} {x.why}", c):
                    errs.append(
                        f"{x.recommendation_id}: name the {'/'.join(k for k in c.entities if k not in ('date', 'dates', 'month'))} specifically"
                    )
                # numbers must be facts of THIS candidate (not just of any candidate)
                own = check([x.title, x.action, x.why, *x.numbers_used], scoped[c.key])
                errs += [
                    f"{x.recommendation_id}: {u['text']} is not a fact of this candidate"
                    for u in own.unverified
                ]
            return list(dict.fromkeys(errs))

        facts = self._facts(a, findings, cands)
        scoped = self.scoped_facts(a, findings, cands)
        reg = get_currency_registry().get(a.currency)
        fx_dates = [r["rate_date"] for r in a.forecast.fx_rates if r.get("rate_date")]
        variables = {
            "as_of": a.forecast.as_of.isoformat(),
            "currency": a.currency,
            "symbol": reg.symbol,
            "locale": reg.locale,
            "fx_asof": max(fx_dates) if fx_dates else "n/a (no foreign-currency items)",
            "max_items": want,
            "candidates": _json(
                [
                    c.public(cid) | {"finding_ids": [fid_of[f] for f in c.finding_ids]}
                    for cid, c in by_id.items()
                ]
            ),
            "findings": _json(
                [
                    {"id": fid_of[fid], "title": f_index[fid].title, "detail": f_index[fid].body}
                    for fid in fid_of
                    if fid in f_index
                ]
            ),
            "goals": _json(a.forecast.goals) if a.forecast.goals else "none",
            "dismissed": _json([{"key": k, "reason": v} for k, v in a.dismissed.items()])
            if a.dismissed
            else "none",
        }
        variables["memories"] = []
        if self.llm is not None and self.llm.available:
            from finpilot.memory.retrieval import recall

            # preferences / plans / decisions relevant to what is on the table (DYNAMIC section)
            about = " ".join(
                f"{c.kind.replace('_', ' ')} {' '.join(map(str, c.entities.values()))}"
                for c in cands
            )
            variables["memories"] = [
                {"kind": m["kind"], "text": m["text"]}
                for m in recall(a.s, user_id, about[:500], self.llm)
            ]
        if self.llm is None or not self.llm.available:
            r = fallback()
            rep = check([t for x in r.recommendations for t in (x.title, x.action, x.why)], facts)
            for x in r.recommendations:  # back to real finding ids
                x.cited_finding_ids = [real_of[f] for f in x.cited_finding_ids if f in real_of]
            return (
                r.recommendations,
                GuardResult(rep, 0, False, True, [["degraded: Mesh unavailable"]]),
                by_id,
                "template",
            )
        out, guard = guarded_json(
            self.llm,
            PROMPT,
            variables,
            AdviceResponse,
            facts=facts,
            texts=lambda r: [
                t for x in r.recommendations for t in (x.title, x.action, x.why, *x.numbers_used)
            ],
            fallback=fallback,
            validate=validate,
            agent="advisor",
            user_id=user_id,
            cache_scope=str(user_id),
        )
        for x in out.recommendations:  # back to real finding ids
            x.cited_finding_ids = [real_of[f] for f in x.cited_finding_ids if f in real_of]
        return out.recommendations, guard, by_id, "template" if guard.fallback_used else "llm"

    def _template(
        self, cid: str, c: Candidate, a: AdviceInputs, fid_of: dict[str, str]
    ) -> AdviceItem:
        ctx = {**c.facts, "names": c.entities, "cur": a.currency}
        if not isinstance(ctx.get("simulation"), dict):
            ctx["simulation"] = {}
        return AdviceItem(
            recommendation_id=cid,
            title=render_snippet("advice", f"{c.kind}.title", **ctx)[:120],
            action=render_snippet("advice", f"{c.kind}.action", **ctx)[:400],
            why=render_snippet("advice", f"{c.kind}.why", **ctx)[:800],
            cited_finding_ids=[fid_of[f] for f in c.finding_ids],
        )

    # ------------------------------------------------------------------ persistence
    def _persist(
        self,
        s: Session,
        a: AdviceInputs,
        run_id: str,
        items: list[AdviceItem],
        by_id: dict[str, Candidate],
        guard: GuardResult | None,
        generated_by: str,
        trace_id: str | None,
        scoped: dict[str, FactSet],
    ) -> list[dict[str, Any]]:
        s.execute(
            text(
                "UPDATE recommendations SET status = 'superseded' WHERE user_id = :u AND status = 'proposed'"
            ),
            {"u": a.user_id},
        )
        dv = s.execute(
            text("SELECT data_version FROM users WHERE id = :u"), {"u": a.user_id}
        ).scalar_one()
        prompt = get_registry().get(PROMPT).meta
        rows = []
        for priority, x in enumerate(items, start=1):
            c = by_id[x.recommendation_id]
            rid = s.execute(
                text("""INSERT INTO recommendations (user_id, run_id, candidate_key, kind, trigger_kind, title, action, why,
                        est_monthly_impact, est_total_impact, currency, priority, generated_by, data_version)
                        VALUES (:u, :run, :key, :kind, :tk, :title, :action, :why, :m, :t, :cur, :p, :gen, :dv) RETURNING id"""),
                {
                    "u": a.user_id,
                    "run": run_id,
                    "key": c.key[:300],
                    "kind": c.kind,
                    "tk": c.trigger_kind,
                    "title": x.title,
                    "action": x.action,
                    "why": x.why,
                    "m": c.est_monthly_impact,
                    "t": c.est_total_impact,
                    "cur": a.currency,
                    "p": priority,
                    "gen": generated_by,
                    "dv": dv,
                },
            ).scalar_one()
            own = check([x.title, x.action, x.why], scoped[c.key])
            report = {**(guard.as_dict() if guard else {}), "this_recommendation": own.as_dict()}
            s.execute(
                text("""INSERT INTO decision_trail (user_id, recommendation_id, trigger_kind, trigger_finding_ids_json,
                        evidence_txn_ids_json, forecast_ref_json, candidate_json, reasoning_text, model, prompt_name,
                        prompt_version, grounding_report_json, langfuse_trace_id)
                        VALUES (:u, :r, :tk, CAST(:f AS jsonb), CAST(:e AS jsonb), CAST(:fr AS jsonb), CAST(:c AS jsonb),
                        :why, :model, :pn, :pv, CAST(:g AS jsonb), :tid)"""),
                {
                    "u": a.user_id,
                    "r": rid,
                    "tk": c.trigger_kind,
                    "f": json.dumps(x.cited_finding_ids or c.finding_ids),
                    "e": json.dumps(c.evidence_txn_ids),
                    "fr": json.dumps(
                        {
                            "forecast_run_id": a.forecast.id,
                            "as_of": a.forecast.as_of,
                            "horizon_days": a.forecast.horizon,
                            "baseline": a.base_outcome.model_dump(mode="json"),
                            "scenario": c.scenario,
                            "simulation": c.simulation,
                        },
                        default=str,
                    ),
                    "c": json.dumps(
                        {
                            "key": c.key,
                            "kind": c.kind,
                            "entities": c.entities,
                            "facts": c.facts,
                            "score": c.score,
                        },
                        default=str,
                    ),
                    "why": x.why,
                    "model": (guard.model if guard and generated_by == "llm" else None),
                    "pn": f"{PROMPT}" if generated_by == "llm" else "templates/messages/advice.yml",
                    "pv": prompt.version if generated_by == "llm" else None,
                    "g": json.dumps(report, default=str),
                    "tid": trace_id,
                },
            )
            rows.append(
                {
                    "id": str(rid),
                    "priority": priority,
                    "kind": c.kind,
                    "candidate_key": c.key,
                    "title": x.title,
                    "action": x.action,
                    "why": x.why,
                    "est_monthly_impact": c.est_monthly_impact,
                    "est_total_impact": c.est_total_impact,
                    "trigger_kind": c.trigger_kind,
                    "cited_finding_ids": x.cited_finding_ids,
                    "generated_by": generated_by,
                }
            )
        return rows


# ---------------------------------------------------------------------------- service functions
def list_recommendations(
    s: Session, user_id: uuid.UUID, include_all: bool = False
) -> dict[str, Any]:
    rows = [
        dict(r)
        for r in s.execute(
            text(f"""SELECT r.*, t.langfuse_trace_id FROM recommendations r LEFT JOIN decision_trail t ON t.recommendation_id = r.id
                 WHERE r.user_id = :u {"" if include_all else "AND r.status IN ('proposed','accepted')"}
                   AND r.run_id = (SELECT run_id FROM recommendations WHERE user_id = :u ORDER BY created_at DESC LIMIT 1)
                 ORDER BY r.priority"""),  # noqa: S608 — fixed clause choice
            {"u": user_id},
        ).mappings()
    ]
    dv = s.execute(
        text("SELECT data_version FROM users WHERE id = :u"), {"u": user_id}
    ).scalar_one()
    return {
        "recommendations": rows,
        "stale": bool(rows) and rows[0]["data_version"] != dv,
        "empty": not rows,
    }


def get_trail(s: Session, user_id: uuid.UUID, rec_id: uuid.UUID) -> dict[str, Any]:
    row = (
        s.execute(
            text("""SELECT r.*, t.trigger_kind AS trail_trigger, t.trigger_finding_ids_json, t.evidence_txn_ids_json,
                       t.forecast_ref_json, t.candidate_json, t.reasoning_text, t.model, t.prompt_name, t.prompt_version,
                       t.grounding_report_json, t.langfuse_trace_id, t.created_at AS trail_created_at
                FROM recommendations r JOIN decision_trail t ON t.recommendation_id = r.id
                WHERE r.id = :id AND r.user_id = :u"""),
            {"id": rec_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise AdviceError("Recommendation not found.", 404)
    out = dict(row)
    ids = out["trigger_finding_ids_json"] or []
    out["findings"] = [
        dict(f)
        for f in s.execute(
            text("""SELECT id, type, kind, severity, title, body, evidence_txn_ids FROM analysis_findings
                WHERE user_id = :u AND id = ANY(CAST(:ids AS uuid[]))"""),
            {"u": user_id, "ids": ids},
        ).mappings()
    ]
    return out


class AdviceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def decide(
    s: Session, user_id: uuid.UUID, rec_id: uuid.UUID, action: str, reason: str | None = None
) -> dict[str, Any]:
    if action not in ("accept", "dismiss"):
        raise AdviceError("Unknown action.", 404)
    # A card may have been superseded by a background refresh while the user read it: the decision
    # still counts (dismissals are remembered by candidate key) and also applies to the same
    # advice in the newest run.
    params = {
        "st": "accepted" if action == "accept" else "dismissed",
        "reason": ((reason or "").strip()[:500] or None) if action == "dismiss" else None,
        "id": rec_id,
        "u": user_id,
    }
    row = s.execute(
        text("""UPDATE recommendations SET status = :st, dismiss_reason = :reason, decided_at = now()
                WHERE id = :id AND user_id = :u RETURNING id, status, candidate_key, title"""),
        params,
    ).first()
    if row is None:
        raise AdviceError("Recommendation not found.", 404)
    s.execute(
        text("""UPDATE recommendations SET status = :st, dismiss_reason = :reason, decided_at = now()
                WHERE user_id = :u AND candidate_key = :k AND status = 'proposed' AND id <> :id"""),
        {**params, "k": row.candidate_key},
    )
    from finpilot.tasks.memory_tasks import schedule_remember_decision

    # long-term memory: the advisor and the chat agent recall it (memory writer, decision slot)
    schedule_remember_decision(
        user_id,
        str(row.id),
        row.title,
        action,
        row.candidate_key,
        str(params["reason"]) if params["reason"] else None,
    )
    return {"id": str(row.id), "status": row.status}


def feedback(
    s: Session, user_id: uuid.UUID, rec_id: uuid.UUID, value: str | None
) -> dict[str, Any]:
    if value not in ("up", "down", None):
        raise AdviceError("Feedback must be up or down.", 422)
    row = s.execute(
        text(
            "UPDATE recommendations SET feedback = :v WHERE id = :id AND user_id = :u RETURNING id, (SELECT langfuse_trace_id FROM decision_trail WHERE recommendation_id = :id)"
        ),
        {"v": value, "id": rec_id, "u": user_id},
    ).first()
    if row is None:
        raise AdviceError("Recommendation not found.", 404)
    if row[1] and value:
        from finpilot.observability.langfuse import client, langfuse_enabled

        if langfuse_enabled():
            try:
                client().create_score(
                    trace_id=row[1], name="user_feedback", value=1.0 if value == "up" else 0.0
                )
            except Exception:
                log.debug("feedback score failed", exc_info=True)
    return {"id": str(row[0]), "feedback": value}


__all__ = [
    "AdviceError",
    "Advisor",
    "AdvisorRun",
    "decide",
    "feedback",
    "get_trail",
    "list_recommendations",
]
