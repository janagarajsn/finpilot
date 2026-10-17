"""Authentication flows. Sync; all writes go through tenant-scoped transactions (RLS).

Pre-authentication lookups use SECURITY DEFINER functions (`auth_lookup_user`,
`auth_refresh_owner`, `auth_reset_owner`, `auth_user_count`), never a bypass role.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
import uuid
import zoneinfo
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.config import get_settings
from finpilot.db.session import get_engine, user_session
from finpilot.delivery.email import send_email
from finpilot.money import get_currency_registry
from finpilot.redis_client import get_redis
from finpilot.security.passwords import (
    WeakPassword,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from finpilot.security.tokens import create_access_token, hash_token, new_opaque_token
from finpilot.templating import render_message

log = logging.getLogger(__name__)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
REGISTER_LOCK_ID = 727_002
REFRESH_REUSE_GRACE_S = 10  # parallel requests may present the same refresh token


class AuthError(Exception):
    status = 400
    code = "auth_error"


class InvalidCredentials(AuthError):
    status, code = 401, "invalid_credentials"


class AccountLocked(AuthError):
    status, code = 423, "account_locked"

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many failed attempts. Try again in {max(retry_after // 60, 1)} min.")
        self.retry_after = retry_after


class EmailTaken(AuthError):
    status, code = 409, "email_taken"


class InvalidInput(AuthError):
    status, code = 422, "invalid_input"


class InvalidRefresh(AuthError):
    status, code = 401, "invalid_refresh_token"


class RefreshReuse(InvalidRefresh):
    code = "refresh_token_reuse"


class InvalidResetToken(AuthError):
    status, code = 400, "invalid_reset_token"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    refresh_expires_at: dt.datetime
    user_id: uuid.UUID
    role: str


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None = None
    user_agent: str | None = None


# ---------------------------------------------------------------------------- helpers
def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL.match(e) or len(e) > 254:
        raise InvalidInput("Enter a valid email address.")
    return e


def validate_profile(currency: str | None = None, timezone: str | None = None) -> None:
    if currency is not None and not get_currency_registry().is_supported(currency):
        raise InvalidInput(f"Unsupported currency {currency!r}.")
    if timezone is not None and timezone not in zoneinfo.available_timezones():
        raise InvalidInput(f"Unknown timezone {timezone!r}.")


def _event(
    s: Session, kind: str, user_id: uuid.UUID | None, client: ClientInfo, **detail: Any
) -> None:
    import json

    s.execute(
        text(
            "INSERT INTO auth_events (user_id, kind, ip, user_agent, detail) "
            "VALUES (:u, :k, CAST(:ip AS inet), :ua, CAST(:d AS jsonb))"
        ),
        {
            "u": user_id,
            "k": kind,
            "ip": _ip(client.ip),
            "ua": (client.user_agent or "")[:300],
            "d": json.dumps(detail, default=str),
        },
    )


def _ip(ip: str | None) -> str | None:
    import ipaddress

    try:
        return str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        return None


def _anon_event(kind: str, client: ClientInfo, **detail: Any) -> None:
    with get_engine().begin() as c:
        s = Session(bind=c)
        _event(s, kind, None, client, **detail)
        s.flush()


def _issue(
    s: Session,
    user_id: uuid.UUID,
    role: str,
    client: ClientInfo,
    family_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> TokenPair:
    token = new_opaque_token()
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=get_settings().refresh_token_ttl_days)
    s.execute(
        text(
            "INSERT INTO refresh_tokens (user_id, family_id, token_hash, parent_id, expires_at, "
            "user_agent, ip) VALUES (:u, :f, :h, :p, :e, :ua, CAST(:ip AS inet))"
        ),
        {
            "u": user_id,
            "f": family_id or uuid.uuid4(),
            "h": hash_token(token),
            "p": parent_id,
            "e": expires,
            "ua": (client.user_agent or "")[:300],
            "ip": _ip(client.ip),
        },
    )
    return TokenPair(create_access_token(user_id, role), token, expires, user_id, role)


def revoke_access_before(user_id: uuid.UUID) -> None:
    """Access tokens issued before now become invalid (password change, reuse, deletion)."""
    try:
        ttl = get_settings().access_token_ttl_min * 60 + 60
        get_redis().set(f"sess:rb:{user_id}", int(time.time() * 1000), ex=ttl)
    except Exception:
        log.warning("could not set revocation hint for %s", user_id, exc_info=True)


def _revoke_all_refresh(s: Session, user_id: uuid.UUID) -> None:
    s.execute(
        text(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"
        ),
        {"u": user_id},
    )


# ---------------------------------------------------------------------------- flows
def register(
    email: str,
    password: str,
    name: str,
    reporting_currency: str = "INR",
    timezone: str = "Asia/Kolkata",
    client: ClientInfo | None = None,
    as_demo: bool = False,
) -> TokenPair:
    client = client or ClientInfo()
    email = normalize_email(email)
    name = (name or "").strip()
    if not 1 <= len(name) <= 100:
        raise InvalidInput("Name is required (max 100 characters).")
    reporting_currency = (reporting_currency or "INR").upper()
    validate_profile(reporting_currency, timezone)
    try:
        validate_password(password, email=email, name=name)
    except WeakPassword as e:
        raise InvalidInput(str(e)) from e
    pw_hash = hash_password(password)
    uid = uuid.uuid4()
    with user_session(uid) as s:
        s.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": REGISTER_LOCK_ID})
        if s.execute(text("SELECT 1 FROM auth_lookup_user(:e)"), {"e": email}).first():
            raise EmailTaken("An account with this email already exists.")
        # Admin: listed in ADMIN_EMAILS, or the first real user to register while no admin exists
        # (demo accounts are created as plain users, so they never take this slot).
        no_admin = s.execute(text("SELECT auth_admin_count()")).scalar_one() == 0
        role = (
            "admin"
            if (no_admin and not as_demo) or email in get_settings().admin_email_set
            else "user"
        )
        s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, name, reporting_currency, timezone, role) "
                "VALUES (:id, :e, :h, :n, :c, :tz, :r)"
            ),
            {
                "id": uid,
                "e": email,
                "h": pw_hash,
                "n": name,
                "c": reporting_currency,
                "tz": timezone,
                "r": role,
            },
        )
        s.execute(text("INSERT INTO user_settings (user_id) VALUES (:u)"), {"u": uid})
        s.execute(
            text(
                "INSERT INTO accounts (user_id, name, type, currency) VALUES (:u, 'Main', 'bank', :c)"
            ),
            {"u": uid, "c": reporting_currency},
        )
        _event(s, "register", uid, client, role=role)
        return _issue(s, uid, role, client)


def _lock_minutes(failures: int) -> int:
    st = get_settings()
    over = failures - st.lockout_threshold
    if over < 0:
        return 0
    return min(st.lockout_base_minutes * (2**over), st.lockout_max_minutes)


def login(email: str, password: str, client: ClientInfo | None = None) -> TokenPair:
    client = client or ClientInfo()
    try:
        email = normalize_email(email)
    except InvalidInput:
        verify_password(None, password)  # constant-ish time
        raise InvalidCredentials("Incorrect email or password.") from None
    with get_engine().connect() as c:
        row = c.execute(text("SELECT * FROM auth_lookup_user(:e)"), {"e": email}).mappings().first()
    now = dt.datetime.now(dt.UTC)
    if row is None:
        verify_password(None, password)
        _anon_event(
            "login_failed", client, email_sha=hashlib.sha256(email.encode()).hexdigest()[:16]
        )
        raise InvalidCredentials("Incorrect email or password.")
    uid: uuid.UUID = row["id"]
    if row["locked_until"] and row["locked_until"] > now:
        raise AccountLocked(int((row["locked_until"] - now).total_seconds()))
    ok = verify_password(row["password_hash"], password)
    with user_session(uid) as s:
        if not ok:
            failures = row["failed_login_count"] + 1
            minutes = _lock_minutes(failures)
            s.execute(
                text("UPDATE users SET failed_login_count = :f, locked_until = :l WHERE id = :u"),
                {
                    "f": failures,
                    "l": now + dt.timedelta(minutes=minutes) if minutes else None,
                    "u": uid,
                },
            )
            _event(
                s,
                "locked" if minutes else "login_failed",
                uid,
                client,
                failures=failures,
                lock_minutes=minutes,
            )
        else:
            params: dict[str, Any] = {"u": uid}
            rehash = ""
            if needs_rehash(row["password_hash"]):
                rehash, params["h"] = ", password_hash = :h", hash_password(password)
            s.execute(
                text(
                    f"UPDATE users SET failed_login_count = 0, locked_until = NULL{rehash} WHERE id = :u"  # noqa: S608 — constant fragment
                ),
                params,
            )
            _event(s, "login_ok", uid, client)
            return _issue(s, uid, row["role"], client)
    raise InvalidCredentials("Incorrect email or password.")


def refresh(refresh_token: str, client: ClientInfo | None = None) -> TokenPair:
    client = client or ClientInfo()
    if not refresh_token:
        raise InvalidRefresh("Missing refresh token.")
    h = hash_token(refresh_token)
    with get_engine().connect() as c:
        row = c.execute(text("SELECT * FROM auth_refresh_owner(:h)"), {"h": h}).mappings().first()
    if row is None:
        raise InvalidRefresh("Invalid refresh token.")
    uid, now = row["user_id"], dt.datetime.now(dt.UTC)
    reused = False
    with user_session(uid) as s:
        if row["revoked_at"] is not None or row["expires_at"] <= now:
            raise InvalidRefresh("Refresh token expired or revoked.")
        claimed = s.execute(
            text(
                "UPDATE refresh_tokens SET used_at = now() WHERE id = :id AND used_at IS NULL RETURNING id"
            ),
            {"id": row["id"]},
        ).first()
        if claimed is None:
            used_at = s.execute(
                text("SELECT used_at FROM refresh_tokens WHERE id = :id"), {"id": row["id"]}
            ).scalar_one()
            grace = dt.timedelta(seconds=REFRESH_REUSE_GRACE_S)
            if used_at is not None and now - used_at <= grace:
                # Concurrent refresh from the same client (parallel requests): issue a sibling
                # token in the same family instead of treating it as theft.
                role = s.execute(
                    text("SELECT role FROM users WHERE id = :u"), {"u": uid}
                ).scalar_one()
                return _issue(s, uid, role, client, family_id=row["family_id"], parent_id=row["id"])
            # Already used outside the grace window → token theft suspected: revoke the family.
            s.execute(
                text(
                    "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = :f AND revoked_at IS NULL"
                ),
                {"f": row["family_id"]},
            )
            _event(s, "refresh_reuse", uid, client, family_id=str(row["family_id"]))
            reused = True
        else:
            role = s.execute(text("SELECT role FROM users WHERE id = :u"), {"u": uid}).scalar_one()
            _event(s, "refresh", uid, client)
            return _issue(s, uid, role, client, family_id=row["family_id"], parent_id=row["id"])
    if reused:
        revoke_access_before(uid)
        raise RefreshReuse(
            "Refresh token reuse detected; all sessions in this family were revoked."
        )
    raise InvalidRefresh("Invalid refresh token.")


def logout(
    refresh_token: str | None, access_jti: str | None = None, client: ClientInfo | None = None
) -> None:
    client = client or ClientInfo()
    if access_jti:
        try:
            get_redis().set(
                f"sess:deny:{access_jti}", 1, ex=get_settings().access_token_ttl_min * 60 + 60
            )
        except Exception:
            log.warning("could not deny-list access token", exc_info=True)
    if not refresh_token:
        return
    with get_engine().connect() as c:
        row = (
            c.execute(
                text("SELECT * FROM auth_refresh_owner(:h)"), {"h": hash_token(refresh_token)}
            )
            .mappings()
            .first()
        )
    if row is None:
        return
    with user_session(row["user_id"]) as s:
        s.execute(
            text(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = :f AND revoked_at IS NULL"
            ),
            {"f": row["family_id"]},
        )
        _event(s, "logout", row["user_id"], client)


def change_password(
    user_id: uuid.UUID, current: str, new: str, client: ClientInfo | None = None
) -> TokenPair:
    client = client or ClientInfo()
    with user_session(user_id) as s:
        u = (
            s.execute(
                text("SELECT email, name, password_hash, role FROM users WHERE id = :u"),
                {"u": user_id},
            )
            .mappings()
            .one()
        )
        if not verify_password(u["password_hash"], current):
            raise InvalidCredentials("Current password is incorrect.")
        try:
            validate_password(new, email=u["email"], name=u["name"])
        except WeakPassword as e:
            raise InvalidInput(str(e)) from e
        if verify_password(u["password_hash"], new):
            raise InvalidInput("New password must differ from the current one.")
        s.execute(
            text("UPDATE users SET password_hash = :h, password_changed_at = now() WHERE id = :u"),
            {"h": hash_password(new), "u": user_id},
        )
        _revoke_all_refresh(s, user_id)
        _event(s, "password_changed", user_id, client)
        revoke_access_before(user_id)  # BEFORE issuing, so the new access token stays valid
        time.sleep(0.002)  # guarantee the new token's iat_ms is strictly after the cutoff
        return _issue(s, user_id, u["role"], client)


def forgot_password(email: str, client: ClientInfo | None = None) -> None:
    """Always succeeds from the caller's view (no account enumeration)."""
    client = client or ClientInfo()
    try:
        email = normalize_email(email)
    except InvalidInput:
        return
    with get_engine().connect() as c:
        row = c.execute(text("SELECT id FROM auth_lookup_user(:e)"), {"e": email}).first()
    if row is None:
        _anon_event("password_reset_requested", client, known=False)
        return
    uid = row[0]
    token = new_opaque_token()
    ttl = get_settings().password_reset_ttl_min
    with user_session(uid) as s:
        name = s.execute(text("SELECT name FROM users WHERE id = :u"), {"u": uid}).scalar_one()
        s.execute(
            text(
                "UPDATE password_reset_tokens SET used_at = now() WHERE user_id = :u AND used_at IS NULL"
            ),
            {"u": uid},
        )
        s.execute(
            text(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (:u, :h, :e)"
            ),
            {
                "u": uid,
                "h": hash_token(token),
                "e": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=ttl),
            },
        )
        _event(s, "password_reset_requested", uid, client, known=True)
    url = f"{get_settings().app_base_url.rstrip('/')}/reset-password?token={token}"
    try:
        send_email(
            email,
            render_message("password_reset.subject.j2").strip(),
            render_message("password_reset.txt.j2", name=name, reset_url=url, ttl_minutes=ttl),
        )
    except Exception:  # same answer either way (no account enumeration); the failure is logged
        log.exception("password reset email could not be sent")


def reset_password(token: str, new_password: str, client: ClientInfo | None = None) -> None:
    client = client or ClientInfo()
    with get_engine().connect() as c:
        row = (
            c.execute(text("SELECT * FROM auth_reset_owner(:h)"), {"h": hash_token(token or "")})
            .mappings()
            .first()
        )
    now = dt.datetime.now(dt.UTC)
    if row is None or row["used_at"] is not None or row["expires_at"] <= now:
        raise InvalidResetToken("This reset link is invalid or has expired.")
    uid = row["user_id"]
    with user_session(uid) as s:
        u = (
            s.execute(text("SELECT email, name FROM users WHERE id = :u"), {"u": uid})
            .mappings()
            .one()
        )
        try:
            validate_password(new_password, email=u["email"], name=u["name"])
        except WeakPassword as e:
            raise InvalidInput(str(e)) from e
        claimed = s.execute(
            text(
                "UPDATE password_reset_tokens SET used_at = now() WHERE id = :id AND used_at IS NULL RETURNING id"
            ),
            {"id": row["id"]},
        ).first()
        if claimed is None:
            raise InvalidResetToken("This reset link is invalid or has expired.")
        s.execute(
            text(
                "UPDATE users SET password_hash = :h, password_changed_at = now(), "
                "failed_login_count = 0, locked_until = NULL WHERE id = :u"
            ),
            {"h": hash_password(new_password), "u": uid},
        )
        _revoke_all_refresh(s, uid)
        _event(s, "password_reset_done", uid, client)
    revoke_access_before(uid)
