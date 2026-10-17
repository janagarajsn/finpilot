"""`make seed-demo`: four ready-to-explore accounts, with data from data/sample/ (6+ months each).

| Account                  | Role  | Reporting | Data                          |
|--------------------------|-------|-----------|-------------------------------|
| admin@finpilot.local     | admin | INR       | none (try "Load sample data") |
| inr@finpilot.local       | user  | INR       | rupees only                   |
| usd@finpilot.local       | user  | USD       | dollars only                  |
| mixed@finpilot.local     | user  | INR       | rupees and dollars            |

Password for all four: DEMO_PASSWORD (default below; dev only). Re-running is safe: existing
accounts are kept and data is loaded only where it isn't yet. Refuses to run in production with
the default password.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import text

from finpilot.auth import service as auth
from finpilot.categories.seed import sync_system_categories
from finpilot.config import get_settings
from finpilot.db.session import get_engine, system_session, user_session
from finpilot.fx.service import build_fx_service
from finpilot.ingest.sample_data import SampleDataError, load_sample

DEFAULT_PASSWORD = "FinPilot-demo-2026!"  # noqa: S105 (dev demo accounts; refused in prod)
# email, name, reporting currency, timezone, role, dataset
ACCOUNTS = [
    ("admin@finpilot.local", "Admin", "INR", "Asia/Kolkata", "admin", None),
    ("inr@finpilot.local", "Asha", "INR", "Asia/Kolkata", "user", "inr"),
    ("usd@finpilot.local", "Sam", "USD", "America/Chicago", "user", "usd"),
    ("mixed@finpilot.local", "Ravi", "INR", "Asia/Kolkata", "user", "mixed"),
]


def main() -> int:
    password = os.getenv("DEMO_PASSWORD", DEFAULT_PASSWORD)
    if get_settings().is_prod and password == DEFAULT_PASSWORD:
        print("Refusing to create demo accounts in production with the default password.")
        print("Set DEMO_PASSWORD to a strong value first.")
        return 2
    sync_system_categories()
    fx = build_fx_service()
    for email, name, cur, tz, role, dataset in ACCOUNTS:
        with get_engine().connect() as c:
            row = c.execute(text("SELECT id FROM auth_lookup_user(:e)"), {"e": email}).first()
        uid = row[0] if row else auth.register(email, password, name, cur, tz, as_demo=True).user_id
        with system_session("seed-demo") as s:
            s.execute(
                text(
                    "UPDATE users SET role = :r, onboarded_at = coalesce(onboarded_at, now()) "
                    "WHERE id = :u"
                ),
                {"r": role, "u": uid},
            )
        if dataset is None:
            print(f"{email} ({role}): no data")
            continue
        try:
            with user_session(uid) as s:
                out = load_sample(s, uid, fx, dataset)
            print(
                f"{email} ({role}): {out['imported']} transactions, "
                f"{out['recurring_rules']} recurring items ({dataset})"
            )
        except SampleDataError:
            print(f"{email} ({role}): sample data already loaded")
        from finpilot.tasks.pipeline_tasks import schedule_pipeline

        try:
            schedule_pipeline(str(uid), "import")
        except Exception:
            print("  (worker not reachable; insights are computed when you open the pages)")
    print(f"Sign in with any account above and the password {password!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
