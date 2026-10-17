"""Row-Level Security: the database itself isolates tenants, even for buggy queries."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from finpilot.config import get_settings
from finpilot.db.session import get_engine, user_session
from tests.conftest import needs_pg, needs_redis
from tests.helpers import insert_txn, make_user

pytestmark = [needs_pg, needs_redis]
# Tables whose user_id NULL rows are shared, read-only system data (e.g. the category taxonomy).
SHARED_SYSTEM_ROWS = {"categories"}


def admin_engine():
    return create_engine(get_settings().database_admin_url)


def user_tables() -> list[str]:
    with get_engine().connect() as c:
        return [
            r[0]
            for r in c.execute(
                text(
                    "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema = 'public' AND c.column_name = 'user_id' AND t.table_type = 'BASE TABLE'"
                )
            )
        ]


def test_every_user_owned_table_has_rls_and_a_policy():
    tables = set(user_tables()) | {"users"}
    assert {"transactions", "accounts", "refresh_tokens", "user_settings"} <= tables
    with get_engine().connect() as c:
        rows = {
            r[0]: (r[1], r[2])
            for r in c.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            )
        }
    missing = [t for t in tables if not (rows[t][0] and rows[t][1] > 0)]
    assert not missing, f"tables without RLS/policy: {missing}"


def test_app_role_cannot_bypass_rls():
    with get_engine().connect() as c:
        assert (
            c.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).scalar()
            is False
        )
        owner = c.execute(
            text("SELECT tableowner FROM pg_tables WHERE tablename = 'transactions'")
        ).scalar()
        assert owner != c.execute(text("SELECT current_user")).scalar()


@pytest.fixture
def two_users(redis_client):
    a, b = make_user(), make_user()
    ta = insert_txn(a.user_id, description="A coffee")
    tb = insert_txn(b.user_id, description="B rent")
    return a.user_id, b.user_id, ta, tb


def test_unfiltered_buggy_queries_only_see_own_rows(two_users):
    a, _b, ta, tb = two_users
    with user_session(a) as s:
        for t in [*user_tables(), "users", "v_transactions_reporting"]:
            col = "id" if t == "users" else "user_id"
            owners = {r[0] for r in s.execute(text(f"SELECT {col} FROM {t}"))}
            allowed = {a, None} if t in SHARED_SYSTEM_ROWS else {a}
            assert owners <= allowed, f"{t} leaked rows of other users: {owners}"
        ids = {r[0] for r in s.execute(text("SELECT id FROM transactions"))}
        assert ta in ids and tb not in ids


def test_cross_tenant_writes_affect_nothing_or_fail(two_users):
    a, b, _, tb = two_users
    with user_session(a) as s:
        assert (
            s.execute(
                text("UPDATE transactions SET notes = 'pwned' WHERE id = :id"), {"id": tb}
            ).rowcount
            == 0
        )
        assert (
            s.execute(text("DELETE FROM transactions WHERE user_id = :b"), {"b": b}).rowcount == 0
        )
        assert (
            s.execute(text("UPDATE users SET name = 'pwned' WHERE id = :b"), {"b": b}).rowcount == 0
        )
        assert s.execute(text("DELETE FROM accounts WHERE user_id = :b"), {"b": b}).rowcount == 0
    with pytest.raises(DBAPIError, match="row-level security"), user_session(a) as s:
        s.execute(
            text(
                "INSERT INTO transactions (user_id, date, raw_description, amount, currency, fx_rate, "
                "amount_inr, fx_rate_date, fx_source, hash) VALUES (:b, '2024-01-01', 'x', -1, 'INR', 1, -1, "
                "'2024-01-01', 'base', :h)"
            ),
            {"b": b, "h": uuid.uuid4().hex},
        )
    with admin_engine().connect() as c:
        assert (
            c.execute(text("SELECT notes FROM transactions WHERE id = :id"), {"id": tb}).scalar()
            is None
        )


def test_no_tenant_context_sees_nothing(two_users):
    with get_engine().connect() as c:
        assert c.execute(text("SELECT count(*) FROM transactions")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 0


def test_admin_role_bypasses_for_system_jobs(two_users):
    a, b, _, _ = two_users
    with admin_engine().connect() as c:
        owners = {
            r[0]
            for r in c.execute(
                text("SELECT user_id FROM transactions WHERE user_id IN (:a, :b)"), {"a": a, "b": b}
            )
        }
    assert owners == {a, b}


def test_security_definer_functions_not_executable_by_readonly_role():
    url = get_settings().database_url.replace(
        "finpilot_app:finpilot_app", "finpilot_ro:finpilot_ro"
    )
    with create_engine(url).connect() as c, pytest.raises(DBAPIError, match="permission denied"):
        c.execute(text("SELECT * FROM auth_lookup_user('a@b.co')"))


def test_user_deletion_cascades(redis_client):
    u = make_user()
    insert_txn(u.user_id)
    with admin_engine().begin() as c:
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": u.user_id})
        for t in user_tables():
            assert (
                c.execute(
                    text(f"SELECT count(*) FROM {t} WHERE user_id = :u"), {"u": u.user_id}
                ).scalar()
                == 0
            )


def test_reporting_view_converts_and_round_trips(redis_client):
    with get_engine().begin() as c:
        c.execute(text("DELETE FROM fx_rates"))
        c.execute(text("INSERT INTO fx_rates VALUES ('2024-01-05','USD',83.15,'mcp',now())"))
    u = make_user("INR").user_id
    usd = insert_txn(
        u, amount="-12.00", currency="USD", fx_rate="83.15", amount_inr="-997.80", source="mcp"
    )
    inr = insert_txn(u, amount="-1000.00", date=dt.date(2024, 1, 7))  # Sunday → uses Friday's rate
    q = text("SELECT id, amount_reporting, reporting_currency FROM v_transactions_reporting")
    with user_session(u) as s:
        rows = {r[0]: (r[1], r[2]) for r in s.execute(q)}
    assert rows[usd] == (Decimal("-997.80"), "INR") and rows[inr][0] == Decimal("-1000.00")
    with user_session(u) as s:
        s.execute(text("UPDATE users SET reporting_currency = 'USD' WHERE id = :u"), {"u": u})
        rows = {r[0]: (r[1], r[2]) for r in s.execute(q)}
    assert rows[usd][0] == Decimal("-12.00")  # exact original (round trip stable)
    assert rows[inr][0] == (Decimal("-1000") / Decimal("83.15")).quantize(Decimal("0.01"))


def test_system_categories_are_shared_but_not_writable(two_users):
    a, b, _, _ = two_users
    from finpilot.categories import service as cats

    with user_session(a) as s:
        cid = cats.name_index(s, a)["groceries"]
        assert (
            s.execute(text("UPDATE categories SET name = 'x' WHERE id = :c"), {"c": cid}).rowcount
            == 0
        )
        assert s.execute(text("DELETE FROM categories WHERE id = :c"), {"c": cid}).rowcount == 0
        mine = cats.create_category(s, a, {"name": "Secret A", "flow_type": "expense"})
    with user_session(b) as s:
        assert "secret a" not in cats.name_index(s, b)
        assert cats.get_category(s, b, mine["id"]) is None
