"""Production secrets helper (no dependencies; reads/prints .env lines).

  python scripts/prod_env.py generate   → print fresh random values for every secret (append to .env)
  python scripts/prod_env.py check      → fail if .env still has dev defaults or weak values

`make prod-up` runs `check` first, so the stack never starts in prod with a default secret.
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

ENV = Path(".env")
# variable → (generator, known dev defaults that must not be used in prod)
SECRETS: dict[str, tuple[str, set[str]]] = {
    "SECRET_KEY": ("urlsafe48", {"change-me-dev-only"}),
    "POSTGRES_PASSWORD": ("urlsafe24", {"finpilot"}),
    "FINPILOT_APP_DB_PASSWORD": ("urlsafe24", {"finpilot_app"}),
    "FINPILOT_ADMIN_DB_PASSWORD": ("urlsafe24", {"finpilot_admin"}),
    "FINPILOT_RO_DB_PASSWORD": ("urlsafe24", {"finpilot_ro"}),
    "LANGFUSE_SALT": ("urlsafe24", {"mysalt"}),
    "LANGFUSE_ENCRYPTION_KEY": ("hex64", {"0" * 64}),
    "LANGFUSE_NEXTAUTH_SECRET": ("urlsafe32", {"mysecret"}),
    "LANGFUSE_CLICKHOUSE_PASSWORD": ("urlsafe24", {"clickhouse"}),
    "LANGFUSE_MINIO_PASSWORD": ("urlsafe24", {"miniosecret"}),
    "LANGFUSE_REDIS_AUTH": ("urlsafe24", {"myredissecret"}),
    "LANGFUSE_INIT_USER_PASSWORD": ("urlsafe16", {"finpilot-admin-pw"}),
    "LANGFUSE_PUBLIC_KEY": ("pk", {"pk-lf-finpilot-local"}),
    "LANGFUSE_SECRET_KEY": ("sk", {"sk-lf-finpilot-local"}),
}
REQUIRED = ["APP_DOMAIN", "ACME_EMAIL", "MESH_API_KEY"]


def _gen(kind: str) -> str:
    if kind == "hex64":
        return secrets.token_hex(32)
    if kind == "pk":
        return "pk-lf-" + secrets.token_hex(16)
    if kind == "sk":
        return "sk-lf-" + secrets.token_hex(16)
    return secrets.token_urlsafe(int(kind.removeprefix("urlsafe")))


def _read() -> dict[str, str]:
    out = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*(#.*)?$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def main(cmd: str) -> int:
    if cmd == "generate":
        for k, (kind, _) in SECRETS.items():
            print(f"{k}={_gen(kind)}")
        return 0
    env, problems = _read(), []
    for k in REQUIRED:
        if not env.get(k):
            problems.append(f"{k} is not set")
    for k, (kind, defaults) in SECRETS.items():
        v = env.get(k, "")
        if not v or v in defaults:
            problems.append(f"{k} is missing or a dev default")
        elif kind == "hex64" and not re.fullmatch(r"[0-9a-f]{64}", v):
            problems.append(f"{k} must be 64 hex characters")
        elif len(v) < 16:
            problems.append(f"{k} is too short")
    if env.get("APP_DOMAIN") in ("localhost", "127.0.0.1"):
        print(
            "note: APP_DOMAIN=localhost uses Caddy's internal CA (browser warning); fine for a trial"
        )
    for p in problems:
        print("✗", p)
    print(
        "prod .env OK" if not problems else f"{len(problems)} problem(s); run `make prod-secrets`"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "check"))
