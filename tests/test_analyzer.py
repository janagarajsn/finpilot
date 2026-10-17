"""Analyzer (DB): anomalies, subscription audit, fx_exposure, persistence, snapshots, tenancy."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from finpilot.agents.analyzer import Analyzer, get_report
from finpilot.db.session import current_data_version, user_session
from finpilot.insights.models import TXN_ANOMALY_TYPES
from finpilot.insights.text import template_types
from finpilot.recurring import detected
from finpilot.recurring.rules import _add_months
from finpilot.transactions import service as txns
from tests.helpers import StubFX, make_user

AS_OF = dt.date(2025, 9, 30)
D = Decimal


def _slug(s, slug: str) -> uuid.UUID:
    return s.execute(
        text("SELECT id FROM categories WHERE slug = :s AND user_id IS NULL"), {"s": slug}
    ).scalar_one()


def add(
    uid,
    day: dt.date,
    amount: str,
    desc: str,
    *,
    cur: str = "INR",
    rate: str | None = None,
    slug: str | None = None,
    kind: str = "expense",
) -> uuid.UUID:
    with user_session(uid) as s:
        tid = txns.create(
            s,
            uid,
            txns.TxnInput(
                kind=kind,  # type: ignore[arg-type]
                amount=D(amount),
                date=day,
                description=desc,
                currency=cur,
                category_id=_slug(s, slug) if slug else None,
                fx_rate=D(rate) if rate else None,
            ),
            StubFX(),
            hash_suffix=uuid.uuid4().hex,
        )
        assert tid is not None
        return tid


def monthly(
    uid,
    desc: str,
    amounts: list[str],
    day: int,
    slug: str,
    cur: str = "INR",
    rates: list[str] | None = None,
    start: dt.date = dt.date(2025, 2, 1),
) -> list[uuid.UUID]:
    return [
        add(
            uid,
            _add_months(start, i, day),
            a,
            desc,
            cur=cur,
            rate=rates[i] if rates else None,
            slug=slug,
        )
        for i, a in enumerate(amounts)
    ]


def run(uid, lookback: int | None = None):
    with user_session(uid) as s:
        return Analyzer().run(s, uid, as_of=AS_OF, lookback_days=lookback)


@pytest.fixture
def uid():
    return make_user("INR").user_id


def test_empty_user_has_an_empty_sparse_report(uid):
    rep = run(uid)
    assert rep.findings == [] and rep.subscriptions == [] and rep.sparse and rep.fx_exposure is None


def test_duplicate_outlier_first_time_and_materiality_floor(uid):
    for i in range(10):  # a regular grocery store, ~₹1,500
        add(
            uid,
            dt.date(2025, 1, 6) + dt.timedelta(days=21 * i),
            str(1400 + 20 * i),
            "BIG BAZAAR KORAMANGALA",
            slug="groceries",
        )
    spike = add(uid, dt.date(2025, 8, 20), "9000", "BIG BAZAAR KORAMANGALA", slug="groceries")
    for i in range(6):  # cheap chai: a 5× spike is still below the ₹2,000 floor
        add(
            uid,
            dt.date(2025, 2, 3) + dt.timedelta(days=30 * i),
            "60",
            "CHAI POINT",
            slug="dining_out",
        )
    add(uid, dt.date(2025, 8, 22), "300", "CHAI POINT", slug="dining_out")
    d1 = add(uid, dt.date(2025, 9, 10), "4000", "CROMA ELECTRONICS", slug="shopping")
    d2 = add(uid, dt.date(2025, 9, 12), "4000", "CROMA ELECTRONICS", slug="shopping")
    add(
        uid, dt.date(2025, 9, 14), "150", "SWIGGY", slug="food_delivery"
    )  # identical small orders = habit
    add(uid, dt.date(2025, 9, 14), "150", "SWIGGY", slug="food_delivery")
    rep = run(uid)
    by_type = {f.type: f for f in rep.findings}
    dup = by_type["duplicate_charge"]
    assert (
        set(dup.evidence_txn_ids) == {str(d1), str(d2)}
        and dup.data["merchant"] == "CROMA ELECTRONICS"
    )
    out = by_type["merchant_outlier"]
    assert out.evidence_txn_ids == [str(spike)] and out.observed == D("9000.00")
    assert out.baseline is not None and out.observed / out.baseline >= 3
    assert (
        "₹9,000.00" in out.body and "robust z-score" in out.body
    )  # states observed, baseline, threshold
    assert not any("CHAI" in f.title or "SWIGGY" in f.title for f in rep.findings)
    # first CROMA payment: ₹4,000 is below 4 × ₹2,000 → not a "large first-time" payment
    assert "first_time_large" not in by_type


def test_price_hike_overlap_and_low_value_never_say_unused(uid):
    netflix = monthly(uid, "NETFLIX.COM", ["649"] * 5 + ["799"] * 3, 5, "subscriptions")
    monthly(uid, "DISNEY HOTSTAR", ["299"] * 8, 12, "subscriptions")
    monthly(uid, "SPOTIFY INDIA", ["119"] * 8, 20, "subscriptions")
    rep = run(uid)
    hike = next(f for f in rep.findings if f.type == "price_hike")
    assert hike.evidence_txn_ids == [str(netflix[5])]
    assert hike.baseline == D("649.00") and hike.observed == D("799") and hike.data["pct"] == 23.1
    overlap = next(f for f in rep.findings if f.type == "subscription_overlap")
    assert overlap.data["group"] == "video_streaming"
    subs = {s.merchant: s for s in rep.subscriptions}
    assert {"price_hike", "overlap"} <= set(subs["NETFLIX"].flags)
    assert subs["SPOTIFY"].flags == [] and rep.subscription_totals["count"] == 3
    assert rep.subscription_totals["monthly"] == D("799.00") + D("299.00") + D("119.00")
    with user_session(uid) as s:
        detected.set_usage(s, uid, uuid.UUID(subs["SPOTIFY"].id), 1)
    rep = run(uid)
    low = next(f for f in rep.findings if f.type == "subscription_low_value")
    assert low.data["basis"] == "user_rating" and "you rated it rarely used" in low.title
    assert all("unused" not in (f.title + f.body).lower() for f in rep.findings)


def test_finding_ids_stable_vanished_inactive_dismissed_kept(uid):
    a = add(uid, dt.date(2025, 9, 10), "5000", "RELIANCE DIGITAL", slug="shopping")
    add(uid, dt.date(2025, 9, 11), "5000", "RELIANCE DIGITAL", slug="shopping")
    f1 = next(f for f in run(uid).findings if f.type == "duplicate_charge")
    f2 = next(f for f in run(uid).findings if f.type == "duplicate_charge")
    assert f1.id == f2.id
    with user_session(uid) as s:
        txns.delete(s, uid, a)
    assert not any(f.type == "duplicate_charge" for f in run(uid).findings)
    with user_session(uid) as s:
        assert (
            s.execute(
                text("SELECT active FROM analysis_findings WHERE id = :i"), {"i": f1.id}
            ).scalar_one()
            is False
        )
    b = add(uid, dt.date(2025, 9, 20), "2500", "DECATHLON", slug="shopping")
    add(uid, dt.date(2025, 9, 21), "2500", "DECATHLON", slug="shopping")
    dup = next(f for f in run(uid).findings if f.type == "duplicate_charge")
    with user_session(uid) as s:
        s.execute(
            text("UPDATE analysis_findings SET dismissed_at = now() WHERE id = :i"), {"i": dup.id}
        )
    assert not any(f.id == dup.id for f in run(uid).findings)  # dismissal survives re-analysis
    assert str(b) in dup.evidence_txn_ids


def test_analysis_never_bumps_data_version_and_snapshot_is_reused(uid):
    monthly(
        uid,
        "NETFLIX.COM",
        ["649"] * 4,
        5,
        "subscriptions",
        start=_add_months(dt.date.today(), -4, 5),
    )
    dv = current_data_version(uid)
    with user_session(uid) as s:
        first = get_report(s, uid)
    assert current_data_version(uid) == dv  # derived tables have no data_version triggers
    with user_session(uid) as s:
        again = get_report(s, uid)
    assert again.model_dump(mode="json") == first.model_dump(
        mode="json"
    )  # served from insight_snapshots
    add(uid, dt.date.today(), "100", "NEW THING")
    with user_session(uid) as s:
        third = get_report(s, uid)
    assert third.data_version > first.data_version


def test_fx_exposure_splits_price_and_rate_exactly(uid):
    rates = ["83.00", "83.50", "84.00", "84.50", "85.00", "85.50"]
    ids = monthly(
        uid,
        "OPENAI *CHATGPT SUBSCR",
        ["20"] * 5 + ["22"],
        5,
        "subscriptions",
        cur="USD",
        rates=rates,
        start=dt.date(2025, 4, 1),
    )
    for i in range(12):
        add(
            uid,
            dt.date(2025, 7, 1) + dt.timedelta(days=7 * i),
            "2000",
            "MORE MEGASTORE",
            slug="groceries",
        )
    rep = run(uid)
    fx = rep.fx_exposure
    assert fx is not None and set(fx["by_currency"]) == {"USD"}
    s = next(x for x in fx["series"] if x["merchant"] == "OPENAI")
    assert s["first_cost"] == D("1660.00") and s["last_cost"] == D("1881.00")
    assert s["price_effect"] == D("166.00")  # (22 − 20) × 83.00
    assert s["fx_effect"] == D("55.00")  # 22 × (85.50 − 83.00)
    assert s["price_effect"] + s["fx_effect"] == s["last_cost"] - s["first_cost"]
    assert s["first_txn_id"] == str(ids[0]) and s["last_txn_id"] == str(ids[-1])
    eff = next(f for f in rep.findings if f.type == "fx_series_effect")
    assert "₹55.00" in eff.body and "83.00 INR per USD" in eff.body


def test_category_spike_and_weekend_pattern(uid):
    for m in range(6):
        for k in range(3):
            add(
                uid,
                _add_months(dt.date(2025, 2, 3), m, 3) + dt.timedelta(days=7 * k),
                "1000",
                f"RESTAURANT {k}",
                slug="dining_out",
            )
    for k in range(6):  # September: ₹15,000 on weekends
        add(
            uid,
            dt.date(2025, 9, 6) + dt.timedelta(days=7 * (k % 4) + (k // 4)),
            "2500",
            "TOIT BREWPUB",
            slug="dining_out",
        )
    rep = run(uid)
    spike = next(f for f in rep.findings if f.type == "category_spike")
    assert (
        spike.data["category"] == "Dining Out"
        and spike.observed == D("15000.00")
        and spike.baseline == D("3000.00")
    )
    assert any(f.type == "weekend_spending" for f in rep.findings)
    assert rep.patterns["weekend"]["ratio"] > 1.4


def test_detection_keeps_user_choices_and_promotion(uid):
    start = _add_months(dt.date.today().replace(day=1), -5, 3)
    monthly(uid, "CULT FIT MEMBERSHIP", ["1599"] * 5, 3, "health", start=start)
    monthly(uid, "ACT FIBERNET", ["1179"] * 5, 18, "utilities", start=start)
    with user_session(uid) as s:
        get_report(s, uid)
        sugg = {d["merchant_norm"]: d for d in detected.list_detected(s, uid)}
    assert {"CULT FIT MEMBERSHIP", "ACT FIBERNET"} <= set(sugg)
    with user_session(uid) as s:
        detected.dismiss(s, uid, sugg["CULT FIT MEMBERSHIP"]["id"])
        out = detected.promote(s, uid, sugg["ACT FIBERNET"]["id"])
    assert out["rule"]["frequency"] == "monthly" and out["rule"]["amount"] == D("1179.00")
    assert out["rule"]["next_run_date"] > dt.date.today() and out["detected"]["declared"]
    with user_session(uid) as s:
        get_report(s, uid, refresh=True)
        assert detected.list_detected(s, uid) == []  # dismissed + declared: no more suggestions
        rows = {d["merchant_norm"]: d for d in detected.list_detected(s, uid, include_all=True)}
    assert rows["CULT FIT MEMBERSHIP"]["dismissed"] and rows["ACT FIBERNET"]["promoted_to_rule_id"]


def test_detected_rows_are_tenant_isolated(uid):
    monthly(uid, "NETFLIX.COM", ["649"] * 4, 5, "subscriptions")
    run(uid)
    other = make_user("INR").user_id
    with user_session(other) as s:
        assert s.execute(text("SELECT count(*) FROM detected_recurrences")).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM analysis_findings")).scalar_one() == 0
    with user_session(uid) as s:
        det = s.execute(text("SELECT id FROM detected_recurrences LIMIT 1")).scalar_one()
    with user_session(other) as s, pytest.raises(Exception, match="not found"):
        detected.set_usage(s, other, det, 1)


def test_every_finding_type_has_a_template():
    produced = set(TXN_ANOMALY_TYPES) | {
        "category_spike", "category_trend_up", "weekend_spending", "payday_spike", "impulse_proxy",
        "concentration", "savings_rate_trend", "latte_factor", "subscription_overlap",
        "subscription_low_value", "fx_exposure", "fx_series_effect", "fees_paid", "recurring_not_declared",
        "forecast_shortfall", "goal_gap", "planned_purchase_risk", "goal_ahead",
    }  # fmt: skip
    assert produced <= template_types()
