"""Request principal + tenant-scoped DB session dependencies.

The user id ALWAYS comes from the verified access token (Bearer header or `fp_access`
cookie), never from request bodies or query params.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from finpilot.db.session import user_session
from finpilot.redis_client import get_redis
from finpilot.security.tokens import AccessClaims, InvalidToken, decode_access_token

log = logging.getLogger(__name__)
ACCESS_COOKIE = "fp_access"
REFRESH_COOKIE = "fp_refresh"


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: str
    jti: str
    via: Literal["bearer", "cookie"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _revoked(claims: AccessClaims) -> bool:
    try:
        r = get_redis()
        if r.exists(f"sess:deny:{claims.jti}"):
            return True
        rb = r.get(f"sess:rb:{claims.user_id}")
        return rb is not None and claims.issued_at_ms <= int(rb)
    except Exception:
        log.warning("revocation check unavailable (fail-open)", exc_info=True)
        return False


def principal_from_request(request: Request) -> Principal | None:
    auth = request.headers.get("authorization", "")
    via: Literal["bearer", "cookie"]
    if auth.lower().startswith("bearer "):
        token, via = auth[7:].strip(), "bearer"
    else:
        token, via = request.cookies.get(ACCESS_COOKIE, ""), "cookie"
    if not token:
        return None
    try:
        claims = decode_access_token(token)
    except InvalidToken:
        return None
    if _revoked(claims):
        return None
    return Principal(claims.user_id, claims.role, claims.jti, via)


def get_principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None) or principal_from_request(request)
    if p is None:
        raise HTTPException(
            status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
        )
    request.state.principal = p
    return p


def get_optional_principal(request: Request) -> Principal | None:
    p = getattr(request.state, "principal", None) or principal_from_request(request)
    request.state.principal = p
    return p


def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return p


def get_db(p: Principal = Depends(get_principal)) -> Iterator[Session]:
    """Tenant-scoped transaction: RLS sees only this principal's rows."""
    with user_session(p.user_id) as s:
        yield s
