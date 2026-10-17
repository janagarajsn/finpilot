"""Password hashing (argon2id) and policy."""

from __future__ import annotations

import re
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from finpilot.config import get_settings

_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
# Verified against when the email is unknown, so timing does not reveal account existence.
_DUMMY_HASH = _hasher.hash("finpilot-dummy-password-for-timing")


class WeakPassword(ValueError):
    pass


@lru_cache
def _common() -> frozenset[str]:
    path = get_settings().config_dir / "common_passwords.txt"
    return frozenset(
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def validate_password(password: str, *, email: str | None = None, name: str | None = None) -> None:
    min_len = get_settings().password_min_length
    if len(password) < min_len:
        raise WeakPassword(f"Password must be at least {min_len} characters.")
    if len(password) > 256:
        raise WeakPassword("Password is too long.")
    low = password.lower()
    stem = re.sub(r"[\d\W_]+$", "", low)  # common word + trailing digits/symbols
    if low in _common() or (len(stem) >= 4 and stem in _common()):
        raise WeakPassword("This password is too common. Choose something less guessable.")
    if len(set(password)) < 4:
        raise WeakPassword("Password is too repetitive.")
    if email and low == email.lower():
        raise WeakPassword("Password must not be your email address.")
    if email and email.split("@")[0].lower() in low and len(email.split("@")[0]) >= 4:
        raise WeakPassword("Password must not contain your email name.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
