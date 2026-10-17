"""JWT access tokens (short-lived) and opaque random tokens stored only as hashes."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

import jwt

from finpilot.config import get_settings

ALG = "HS256"
ISSUER = "finpilot"


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str
    jti: str
    issued_at: dt.datetime
    issued_at_ms: int  # millisecond precision for revocation checks (JWT iat is whole seconds)


def _key() -> str:
    return get_settings().secret_key.get_secret_value()


def create_access_token(user_id: uuid.UUID, role: str, *, ttl_min: int | None = None) -> str:
    now = dt.datetime.now(dt.UTC)
    ttl = ttl_min if ttl_min is not None else get_settings().access_token_ttl_min
    payload = {
        "sub": str(user_id),
        "role": role,
        "typ": "access",
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + dt.timedelta(minutes=ttl),
        "jti": secrets.token_hex(8),
        "iat_ms": int(now.timestamp() * 1000),
    }
    return jwt.encode(payload, _key(), algorithm=ALG)


def decode_access_token(token: str) -> AccessClaims:
    try:
        p = jwt.decode(
            token,
            _key(),
            algorithms=[ALG],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e
    if p.get("typ") != "access":
        raise InvalidToken("wrong token type")
    try:
        uid = uuid.UUID(p["sub"])
    except ValueError as e:
        raise InvalidToken("bad subject") from e
    return AccessClaims(
        uid,
        p.get("role", "user"),
        p["jti"],
        dt.datetime.fromtimestamp(p["iat"], dt.UTC),
        int(p.get("iat_ms", p["iat"] * 1000)),
    )


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Keyed hash (HMAC-SHA256): a DB leak alone cannot be used to forge lookups."""
    return hmac.new(_key().encode(), token.encode(), hashlib.sha256).hexdigest()
