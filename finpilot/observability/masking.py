"""PII masking applied to everything exported to Langfuse."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w+])\+?\(?\d[\d\s().-]{7,16}\d(?!\w)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")  # account / card / UPI ref numbers


def hash_user_id(user_id: object) -> str:
    """Stable pseudonymous id: Langfuse only ever sees hashed user ids."""
    return "u_" + hashlib.sha256(str(user_id).encode()).hexdigest()[:16]


def mask_text(text: str) -> str:
    try:  # Presidio first (checksums, context), then the regexes below as a safety net
        from finpilot.privacy.pii import mask

        text = mask(text)[0]
    except Exception:  # never let masking break trace export; the regexes below still apply
        logging.getLogger(__name__).debug("presidio masking failed", exc_info=True)
    text = _EMAIL.sub("<email>", text)
    text = _LONG_DIGITS.sub(lambda m: f"<num:{len(m.group())}d…{m.group()[-2:]}>", text)
    text = _PHONE.sub(
        lambda m: "<phone>" if 10 <= sum(c.isdigit() for c in m.group()) <= 13 else m.group(), text
    )
    return text


def _hash_body(value: str) -> str:
    return f"<redacted sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]} len={len(value)}>"


def make_mask(capture_content: bool):
    """Return a Langfuse `mask` callable (recursive over dict/list payloads)."""

    def _mask(data: Any, **_: Any) -> Any:
        if isinstance(data, str):
            return mask_text(data) if capture_content else _hash_body(data)
        if isinstance(data, dict):
            return {k: _mask(v) for k, v in data.items()}
        if isinstance(data, list | tuple):
            return [_mask(v) for v in data]
        return data

    return _mask
