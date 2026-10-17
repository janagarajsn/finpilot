"""Analyzer agent (deterministic core; an LLM may later NARRATE findings, never compute them).

`Analyzer().run(s, user_id, as_of)`:
1. refreshes detected recurring series (finpilot/recurring/detect.py),
2. loads the reporting-currency frame and runs anomalies, trends, patterns, subscription audit
   and fx_exposure (finpilot/insights/*),
3. renders finding text from templates/messages/findings.yml,
4. persists findings (stable ids via fingerprints; vanished findings → inactive; the user's
   dismissals are kept) and refreshes `insight_snapshots(kind='analysis')`.

`get_report` serves the snapshot while (data_version, reporting_currency, as_of) match, else
re-runs under a per-user advisory lock so concurrent page loads compute once.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.categories.rules import thresholds
from finpilot.insights import anomalies, fx_exposure, patterns, subscriptions
from finpilot.insights.frame import Frame, load_frame
from finpilot.insights.models import AnalysisReport, CategoryTrend, Finding
from finpilot.insights.text import render
from finpilot.recurring import detect
from finpilot.recurring.service import local_today

log = logging.getLogger(__name__)
SNAPSHOT_KIND = "analysis"

_UPSERT = text("""
INSERT INTO analysis_findings (user_id, fingerprint, kind, type, severity, title, body, metric, baseline,
  observed, threshold, unit, currency, evidence_txn_ids, data_json, period_start, period_end, as_of)
VALUES (:u, :fp, :kind, :type, :sev, :title, :body, :metric, :baseline, :observed, :threshold, :unit,
  :currency, CAST(:ev AS jsonb), CAST(:data AS jsonb), :ps, :pe, :as_of)
ON CONFLICT (user_id, fingerprint) DO UPDATE SET
  kind = EXCLUDED.kind, type = EXCLUDED.type, severity = EXCLUDED.severity, title = EXCLUDED.title,
  body = EXCLUDED.body, metric = EXCLUDED.metric, baseline = EXCLUDED.baseline,
  observed = EXCLUDED.observed, threshold = EXCLUDED.threshold, unit = EXCLUDED.unit,
  currency = EXCLUDED.currency, evidence_txn_ids = EXCLUDED.evidence_txn_ids,
  data_json = EXCLUDED.data_json, period_start = EXCLUDED.period_start,
  period_end = EXCLUDED.period_end, as_of = EXCLUDED.as_of, active = true, last_seen_at = now()
RETURNING id, dismissed_at
""")


ANALYZER_KINDS = ("anomaly", "pattern", "subscription", "fx_exposure", "trend")


def upsert_findings(
    s: Session,
    user_id: uuid.UUID,
    findings: list[Finding],
    as_of: dt.date,
    owned_kinds: tuple[str, ...],
) -> list[Finding]:
    """Upsert by fingerprint (ids stay stable), deactivate this owner's findings that vanished,
    and return the ones the user has not dismissed (with ids set)."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda f: f.fingerprint):  # stable lock order
        row = s.execute(
            _UPSERT,
            {
                "u": user_id,
                "fp": f.fingerprint[:300],
                "kind": f.kind,
                "type": f.type,
                "sev": f.severity,
                "title": f.title,
                "body": f.body,
                "metric": f.metric,
                "baseline": f.baseline,
                "observed": f.observed,
                "threshold": f.threshold,
                "unit": f.unit,
                "currency": f.currency,
                "ev": json.dumps(f.evidence_txn_ids),
                "data": json.dumps(_jsonable(f.data)),
                "ps": f.period_start,
                "pe": f.period_end,
                "as_of": as_of,
            },
        ).one()
        f.id = str(row.id)
        if row.dismissed_at is None:
            kept.append(f)
    s.execute(
        text("""UPDATE analysis_findings SET active = false
                WHERE user_id = :u AND active AND NOT (fingerprint = ANY(:fps)) AND kind = ANY(:kinds)"""),
        {"u": user_id, "fps": [f.fingerprint[:300] for f in findings], "kinds": list(owned_kinds)},
    )
    return kept


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


class Analyzer:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or thresholds()

    def run(
        self,
        s: Session,
        user_id: uuid.UUID,
        as_of: dt.date | None = None,
        lookback_days: int | None = -1,
        persist: bool = True,
    ) -> AnalysisReport:
        """lookback_days: -1 = configured default (product), None = whole history (evals)."""
        t0 = time.perf_counter()
        user = (
            s.execute(
                text("SELECT reporting_currency, timezone, data_version FROM users WHERE id = :u"),
                {"u": user_id},
            )
            .mappings()
            .one()
        )
        as_of = as_of or local_today(user["timezone"])
        if lookback_days == -1:
            lookback_days = int(self.cfg["anomalies"]["lookback_days"])
        detect.refresh(s, user_id, as_of)
        fr = load_frame(s, user_id, as_of, user["reporting_currency"], self.cfg, lookback_days)
        report = self._analyze(fr, user_id, user["data_version"])
        if persist:
            self._persist(s, user_id, report)
        report.duration_ms = int((time.perf_counter() - t0) * 1000)
        if persist:
            self._snapshot(s, user_id, report)
        return report

    # ------------------------------------------------------------------ analysis
    def _analyze(self, fr: Frame, user_id: uuid.UUID, data_version: int) -> AnalysisReport:
        findings: list[Finding] = anomalies.run(fr)
        trend_findings, trends = anomalies.category_trends(fr)
        pattern_metrics, pattern_findings = patterns.run(fr)
        subs, sub_totals, sub_findings = subscriptions.audit(fr)
        fx_report, fx_findings = fx_exposure.run(fr)
        findings += trend_findings + pattern_findings + sub_findings + fx_findings
        findings += patterns.recurring_not_declared(fr)
        if (fees := patterns.fees_paid(fr)) is not None:
            findings.append(fees)
        for f in findings:
            render(f)
        hs = fr.history_start
        months = 0.0 if hs is None else round((fr.as_of - hs).days / 30.44, 1)
        sp = fr.spending()
        weekly = (
            (-sp.set_index("date")["amt"]).resample("W-SUN").sum().tail(12)
            if not sp.empty
            else None
        )
        return AnalysisReport(
            user_id=str(user_id),
            as_of=fr.as_of,
            reporting_currency=fr.currency,
            data_version=data_version,
            lookback_start=fr.scan_start,
            history_start=hs,
            months_of_data=months,
            findings=sorted(findings, key=lambda f: f.sort_key),
            subscriptions=subs,
            subscription_totals=sub_totals,
            category_trends=[CategoryTrend(**t) for t in trends],
            weekly_spend=[]
            if weekly is None
            else [
                {"week_ending": d.date().isoformat(), "spend": fr.money(v)}
                for d, v in weekly.items()
            ],
            patterns=pattern_metrics,
            fx_exposure=fx_report,
            recurring=[
                {
                    "id": str(d["id"]),
                    "merchant": d["merchant_norm"],
                    "kind": d["kind"],
                    "period": d["period"],
                    "amount": d["amount_typical"],
                    "currency": d["currency"],
                    "next_expected": d["next_expected"],
                    "status": d["status"],
                    "declared": d["promoted_to_rule_id"] is not None,
                    "dismissed": d["dismissed"],
                    "confidence": float(d["confidence"]),
                }
                for d in fr.detected
            ],
            sparse=months < 2,
        )

    # ------------------------------------------------------------------ persistence
    def _persist(self, s: Session, user_id: uuid.UUID, report: AnalysisReport) -> None:
        report.findings = sorted(
            upsert_findings(s, user_id, report.findings, report.as_of, ANALYZER_KINDS),
            key=lambda f: f.sort_key,
        )

    def _snapshot(self, s: Session, user_id: uuid.UUID, report: AnalysisReport) -> None:
        s.execute(
            text("""
            INSERT INTO insight_snapshots (user_id, kind, data_version, reporting_currency, as_of, payload_json, duration_ms)
            VALUES (:u, :k, :dv, :cur, :as_of, CAST(:p AS jsonb), :ms)
            ON CONFLICT (user_id, kind) DO UPDATE SET data_version = EXCLUDED.data_version,
              reporting_currency = EXCLUDED.reporting_currency, as_of = EXCLUDED.as_of,
              payload_json = EXCLUDED.payload_json, duration_ms = EXCLUDED.duration_ms, created_at = now()"""),
            {
                "u": user_id,
                "k": SNAPSHOT_KIND,
                "dv": report.data_version,
                "cur": report.reporting_currency,
                "as_of": report.as_of,
                "p": report.model_dump_json(),
                "ms": report.duration_ms,
            },
        )


def invalidate(s: Session, user_id: uuid.UUID) -> None:
    """User actions on derived rows (dismiss, usage rating, promote) → recompute on next read."""
    s.execute(
        text(
            "DELETE FROM insight_snapshots WHERE user_id = :u AND kind IN (:k, 'analysis_summary')"
        ),
        {"u": user_id, "k": SNAPSHOT_KIND},
    )


def _fresh(s: Session, user_id: uuid.UUID) -> AnalysisReport | None:
    row = s.execute(
        text("""
        SELECT i.payload_json FROM insight_snapshots i JOIN users u ON u.id = i.user_id
        WHERE i.user_id = :u AND i.kind = :k AND i.data_version = u.data_version
          AND i.reporting_currency = u.reporting_currency AND i.as_of = :today"""),
        {"u": user_id, "k": SNAPSHOT_KIND, "today": _today(s, user_id)},
    ).first()
    return AnalysisReport.model_validate(row[0]) if row else None


def _today(s: Session, user_id: uuid.UUID) -> dt.date:
    tz = s.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id}).scalar_one()
    return local_today(tz)


def get_report(s: Session, user_id: uuid.UUID, refresh: bool = False) -> AnalysisReport:
    if not refresh and (cached := _fresh(s, user_id)) is not None:
        return cached
    # Serialize per user: the second concurrent request waits, then finds the fresh snapshot.
    s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('analysis:' || CAST(:u AS text)))"),
        {"u": user_id},
    )
    if not refresh and (cached := _fresh(s, user_id)) is not None:
        return cached
    return Analyzer().run(s, user_id)
