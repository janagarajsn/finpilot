"""Categories, transactions, data_version triggers, recurring service + materializer (real DB)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from finpilot.categories import service as cats
from finpilot.db.session import current_data_version, user_session
from finpilot.recurring import materializer
from finpilot.recurring import service as rec
from finpilot.transactions import service as txns
from tests.conftest import needs_pg, needs_redis
from tests.helpers import StubFX, make_user

pytestmark = [needs_pg, needs_redis]
D = dt.date


@pytest.fixture
def uid(redis_client):
    return make_user("INR").user_id


def cat_id(s, uid, name):
    return cats.name_index(s, uid)[name.lower()]


def dv(uid) -> int:
    with user_session(uid) as s:
        return s.execute(
            text("SELECT data_version FROM users WHERE id = :u"), {"u": uid}
        ).scalar_one()


# ---------------------------------------------------------------- categories
def test_system_taxonomy_seeded_and_read_only(uid):
    with user_session(uid) as s:
        all_ = cats.list_categories(s, uid)
        assert {"Salary", "Groceries", "Food Delivery", "Cash/ATM"} <= {c["name"] for c in all_}
        groceries = cat_id(s, uid, "Groceries")
        with pytest.raises(cats.CategoryError) as e:
            cats.update_category(s, uid, groceries, {"name": "Food"})
        assert e.value.status == 403
        cats.set_hidden(s, uid, groceries, True)
        assert next(c for c in cats.list_categories(s, uid) if c["id"] == groceries)["hidden"]


def test_custom_category_crud_and_name_clash(uid):
    with user_session(uid) as s:
        pet = cats.create_category(
            s,
            uid,
            {
                "name": " Pet  Care ",
                "flow_type": "expense",
                "nature": "essential_variable",
                "keywords": ["SUPERTAILS", "vet", "vet"],
                "description": "Pet food & vet",
            },
        )
        assert pet["name"] == "Pet Care" and pet["keywords"] == ["SUPERTAILS", "vet"]
        with pytest.raises(cats.CategoryError, match="already exists"):
            cats.create_category(s, uid, {"name": "groceries", "flow_type": "expense"})
        inc = cats.create_category(
            s, uid, {"name": "Rental income", "flow_type": "income", "nature": "fixed"}
        )
        assert inc["nature"] == "n/a"  # non-expense categories have no nature
        upd = cats.update_category(
            s, uid, pet["id"], {"color": "#123456", "keywords": ["supertails"]}
        )
        assert upd["color"] == "#123456" and upd["keywords"] == ["supertails"]


def test_delete_requires_reassign_and_merge_moves_everything(uid):
    fx = StubFX()
    with user_session(uid) as s:
        a = cats.create_category(s, uid, {"name": "Tuition", "flow_type": "expense"})["id"]
        b = cats.create_category(s, uid, {"name": "Classes", "flow_type": "expense"})["id"]
        t = txns.create(
            s,
            uid,
            txns.TxnInput("expense", Decimal("500"), D(2024, 1, 5), "Math tuition", category_id=a),
            fx,
        )
        with pytest.raises(cats.CategoryError) as e:
            cats.delete_category(s, uid, a)
        assert e.value.status == 409
        assert cats.merge_categories(s, uid, a, b) == 1
        assert txns.get(s, uid, t)["category_id"] == b
        assert cats.get_category(s, uid, a) is None


def test_keyword_rules_and_recategorize_preview_apply(uid):
    fx = StubFX()
    with user_session(uid) as s:
        shopping = cat_id(s, uid, "Shopping")
        t1 = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense",
                Decimal("1200"),
                D(2024, 1, 3),
                "SUPERTAILS ORDER 991",
                category_id=shopping,
            ),
            fx,
        )  # user-set: must NOT change
        s.execute(text("UPDATE transactions SET cat_source = 'llm' WHERE id = :t"), {"t": t1})
        t2 = txns.create(
            s,
            uid,
            txns.TxnInput("expense", Decimal("800"), D(2024, 1, 4), "UPI/SUPERTAILS/pet food"),
            fx,
        )
        t3 = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("300"), D(2024, 1, 5), "Vet visit", category_id=shopping
            ),
            fx,
        )
        pet = cats.create_category(
            s, uid, {"name": "Pet Care", "flow_type": "expense", "keywords": ["supertails"]}
        )
        preview = cats.recategorize_preview(s, uid, pet["id"])
        assert {c.transaction_id for c in preview} == {t1, t2}  # t3 is user-set and doesn't match
        assert cats.recategorize_apply(s, uid, pet["id"], [t1, t2, t3]) == 2
        assert txns.get(s, uid, t2)["cat_source"] == "rule"
        # new transactions pick up the keyword immediately
        t4 = txns.create(
            s, uid, txns.TxnInput("expense", Decimal("99"), D(2024, 1, 6), "SUPERTAILS treats"), fx
        )
        assert txns.get(s, uid, t4)["category_name"] == "Pet Care"
        assert any(x["name"] == "Pet Care" for x in cats.suggest(s, uid, "supertails"))


# ---------------------------------------------------------------- transactions + FX
def test_manual_entry_inr_and_foreign_currency(uid):
    fx = StubFX()
    with user_session(uid) as s:
        a = txns.create(
            s, uid, txns.TxnInput("expense", Decimal("250.50"), D(2024, 1, 5), "Chaayos"), fx
        )
        b = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("12"), D(2024, 1, 5), "OPENAI *CHATGPT", currency="USD"
            ),
            fx,
        )
        c = txns.create(
            s,
            uid,
            txns.TxnInput(
                "income",
                Decimal("1000"),
                D(2024, 1, 5),
                "Client",
                currency="USD",
                fx_rate=Decimal("84.00"),
            ),
            fx,
        )
        ra, rb, rc = txns.get(s, uid, a), txns.get(s, uid, b), txns.get(s, uid, c)
    assert (ra["amount"], ra["fx_source"], ra["amount_reporting"]) == (
        Decimal("-250.50"),
        "base",
        Decimal("-250.50"),
    )
    assert (rb["amount"], rb["amount_inr"], rb["fx_source"]) == (
        Decimal("-12.00"),
        Decimal("-997.80"),
        "mcp",
    )
    assert rb["merchant_norm"] == "OPENAI"
    assert (rc["amount_inr"], rc["fx_source"]) == (Decimal("84000.00"), "user_override")


def test_validation_errors(uid):
    with user_session(uid) as s:
        for bad in [
            txns.TxnInput("expense", Decimal("0"), D(2024, 1, 1), "x"),
            txns.TxnInput("expense", Decimal("1"), D(2024, 1, 1), "   "),
            txns.TxnInput("expense", Decimal("1"), D(2024, 1, 1), "x", currency="EUR"),
            txns.TxnInput("expense", Decimal("1"), D(2024, 1, 1), "x", category_id=uuid.uuid4()),
        ]:
            with pytest.raises(txns.TxnError):
                txns.create(s, uid, bad, StubFX())


def test_edit_recomputes_fx_and_category_correction_applies_to_merchant(uid):
    fx = StubFX()
    with user_session(uid) as s:
        t1 = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("12"), D(2024, 1, 5), "SQ *MOONLIGHT ROASTERS", currency="USD"
            ),
            fx,
        )
        t2 = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("7"), D(2024, 1, 9), "SQ *MOONLIGHT ROASTERS #12", currency="USD"
            ),
            fx,
        )
        r = txns.update(s, uid, t1, {"amount": Decimal("15")}, fx)
        assert r["amount"] == Decimal("-15.00") and r["amount_inr"] == Decimal("-1247.25")
        dining = cat_id(s, uid, "Dining Out")
        assert txns.set_category(s, uid, t1, dining, apply_to_merchant=True) == 1
        assert (
            txns.get(s, uid, t2)["category_id"] == dining
            and txns.get(s, uid, t2)["cat_source"] == "memory"
        )
        t3 = txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("5"), D(2024, 2, 1), "SQ *MOONLIGHT ROASTERS", currency="USD"
            ),
            fx,
        )
        assert txns.get(s, uid, t3)["category_id"] == dining  # future rows use merchant memory
        r = txns.override_fx(s, uid, t3, Decimal("90"), fx)
        assert r["amount_inr"] == Decimal("-450.00") and r["fx_source"] == "user_override"


def test_data_version_bumps_atomically_and_mirrors_to_redis(uid, redis_client):
    fx = StubFX()
    v0 = dv(uid)
    with user_session(uid) as s:
        t = txns.create(s, uid, txns.TxnInput("expense", Decimal("1"), D(2024, 1, 1), "x"), fx)
    v1 = dv(uid)
    assert v1 == v0 + 1 and int(redis_client.get(f"dv:{uid}")) == v1
    with user_session(uid) as s:
        s.execute(
            text("UPDATE users SET name = 'n' WHERE id = :u"), {"u": uid}
        )  # not a financial fact
    assert dv(uid) == v1
    with user_session(uid) as s:
        s.execute(text("UPDATE users SET reporting_currency = 'USD' WHERE id = :u"), {"u": uid})
        txns.delete(s, uid, t)
    assert dv(uid) == v1 + 2
    redis_client.delete(f"dv:{uid}")
    assert current_data_version(uid) == v1 + 2  # Postgres fallback re-mirrors
    try:
        with user_session(uid) as s:
            txns.create(s, uid, txns.TxnInput("expense", Decimal("1"), D(2024, 1, 1), "y"), fx)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    assert dv(uid) == v1 + 2  # rolled back together with the write


def test_csv_export_neutralizes_formulas(uid):
    with user_session(uid) as s:
        txns.create(
            s,
            uid,
            txns.TxnInput(
                "expense", Decimal("5"), D(2024, 1, 1), '=HYPERLINK("http://x")', notes="@SUM(A1)"
            ),
            StubFX(),
        )
        out = txns.export_csv(s, uid, txns.TxnFilter())
    assert "'=HYPERLINK" in out and "'@SUM(A1)" in out and ",5.00,expense" in out
    assert ",-5.00," in out  # numeric cells are never prefixed


# ---------------------------------------------------------------- recurring
def test_rule_lifecycle_and_materializer_catch_up_idempotent(uid):
    fx = StubFX()
    with user_session(uid) as s:
        rent = cat_id(s, uid, "Rent/Mortgage")
        today = rec.user_today(s, uid)
        start = today - dt.timedelta(days=65)  # ~2 missed months → catch-up
        r = rec.create_rule(
            s,
            uid,
            {
                "kind": "expense",
                "description": "Rent via NEFT",
                "amount": "25000",
                "frequency": "monthly",
                "start_date": start,
                "category_id": rent,
            },
        )
        assert r["status"] == "active" and len(r["next_occurrences"]) == 5
    res1 = materializer.materialize_user(uid, fx)
    res2 = materializer.materialize_user(uid, fx)  # second tick: nothing new
    with user_session(uid) as s:
        rows = s.execute(
            text(
                "SELECT date, amount, source, category_id FROM transactions "
                "WHERE recurring_rule_id = :r ORDER BY date"
            ),
            {"r": r["id"]},
        ).all()
        rule = rec.get_rule(s, uid, r["id"])
    assert res1.created == len(rows) >= 2 and res2.created == 0
    assert all(
        x.source == "recurring_rule" and x.amount == Decimal("-25000.00") and x.category_id == rent
        for x in rows
    )
    assert rule["next_run_date"] > today and rule["last_materialized_date"] == rows[-1].date


def test_skip_pause_resume_end(uid):
    with user_session(uid) as s:
        today = rec.user_today(s, uid)
        r = rec.create_rule(
            s,
            uid,
            {
                "kind": "expense",
                "description": "Netflix",
                "amount": "649",
                "frequency": "monthly",
                "start_date": today + dt.timedelta(days=3),
            },
        )
        first = r["next_run_date"]
        r = rec.set_status(s, uid, r["id"], "skip-next")
        assert r["next_run_date"] > first and first.isoformat() in r["skip_dates_json"]
        assert rec.set_status(s, uid, r["id"], "pause")["status"] == "paused"
        with pytest.raises(rec.RecurringError):
            rec.set_status(s, uid, r["id"], "pause")
        assert rec.set_status(s, uid, r["id"], "resume")["status"] == "active"
        ended = rec.set_status(s, uid, r["id"], "end")
        assert ended["status"] == "ended" and ended["next_run_date"] is None


def test_repeat_this_links_first_transaction(uid):
    fx = StubFX()
    with user_session(uid) as s:
        today = rec.user_today(s, uid)
        t = txns.create(s, uid, txns.TxnInput("expense", Decimal("199"), today, "Spotify"), fx)
        r = rec.create_rule(
            s,
            uid,
            {
                "kind": "expense",
                "description": "Spotify",
                "amount": "199",
                "frequency": "monthly",
                "start_date": today,
            },
            first_txn_id=t,
        )
        assert r["last_materialized_date"] == today and r["next_run_date"] > today
    assert materializer.materialize_user(uid, fx).created == 0  # today's occurrence already exists


def test_materializer_retries_failed_occurrence(uid):
    class FailingFX(StubFX):
        def rate_to_inr(self, currency, on=None):
            raise RuntimeError("fx down")

    with user_session(uid) as s:
        today = rec.user_today(s, uid)
        r = rec.create_rule(
            s,
            uid,
            {
                "kind": "expense",
                "description": "AWS",
                "amount": "20",
                "currency": "USD",
                "frequency": "monthly",
                "start_date": today - dt.timedelta(days=40),
            },
        )
    bad = materializer.materialize_user(uid, FailingFX())
    with user_session(uid) as s:
        assert rec.get_rule(s, uid, r["id"])["next_run_date"] == r["next_run_date"]  # not advanced
    assert bad.errors == 1
    good = materializer.materialize_user(uid, StubFX())
    assert good.created >= 1 and good.errors == 0
