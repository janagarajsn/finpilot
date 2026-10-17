from __future__ import annotations

import time
import uuid

import jwt
import pytest

from finpilot.security.passwords import (
    WeakPassword,
    hash_password,
    validate_password,
    verify_password,
)
from finpilot.security.tokens import (
    InvalidToken,
    create_access_token,
    decode_access_token,
    hash_token,
    new_opaque_token,
)


@pytest.mark.parametrize(
    "pw",
    [
        "short",
        "password123",
        "qwertyuiop",
        "monkey2024!!",
        "aaaaaaaaaaaa",
        "1234567890",
    ],
)
def test_weak_passwords_rejected(pw):
    with pytest.raises(WeakPassword):
        validate_password(pw)


def test_password_must_not_contain_email_name():
    with pytest.raises(WeakPassword):
        validate_password("priya-rocks-2026", email="priya@example.com")


def test_strong_password_accepted_and_argon2id_roundtrip():
    validate_password("correct horse battery staple", email="a@b.co")
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery staple")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")  # unknown user path


def test_access_token_roundtrip_and_tamper_detection():
    uid = uuid.uuid4()
    tok = create_access_token(uid, "admin")
    claims = decode_access_token(tok)
    assert claims.user_id == uid and claims.role == "admin"
    with pytest.raises(InvalidToken):
        decode_access_token(tok[:-2] + ("A" if tok[-1] != "A" else "B") + tok[-1])
    forged = jwt.encode(
        {
            "sub": str(uid),
            "typ": "access",
            "iss": "finpilot",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "x",
        },
        "other-key",
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        decode_access_token(forged)


def test_expired_and_wrong_type_tokens_rejected():
    with pytest.raises(InvalidToken):
        decode_access_token(create_access_token(uuid.uuid4(), "user", ttl_min=-1))
    from finpilot.config import get_settings

    key = get_settings().secret_key.get_secret_value()
    refreshish = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "typ": "refresh",
            "iss": "finpilot",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "x",
        },
        key,
        "HS256",
    )
    with pytest.raises(InvalidToken):
        decode_access_token(refreshish)


def test_opaque_tokens_are_random_and_hashed_with_key():
    a, b = new_opaque_token(), new_opaque_token()
    assert a != b and len(a) >= 40
    assert hash_token(a) == hash_token(a) != hash_token(b) and a not in hash_token(a)


def test_prod_refuses_weak_secret_and_plain_http():
    from pydantic import ValidationError

    from finpilot.config import Settings

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(app_env="prod", secret_key="change-me", app_base_url="https://x.example")
    with pytest.raises(ValidationError, match="https"):
        Settings(app_env="prod", secret_key="k" * 40, app_base_url="http://x.example")
    assert Settings(app_env="prod", secret_key="k" * 40, app_base_url="https://x.example").is_prod
