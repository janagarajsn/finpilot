"""Telegram delivery and account linking (a plain Bot API adapter: no MCP, per AMENDMENT_1).

Linking: Settings → "Link Telegram" creates a single-use token (only its HMAC is stored) and the
deep link `t.me/<bot>?start=<token>`. When the user presses Start, Telegram delivers
"/start <token>" to our webhook (`POST /telegram/webhook/{secret}`) or, without a public URL, to
the `telegram_poll` task; both call `handle_update`, which binds the chat through the
SECURITY DEFINER function `telegram_bind`. Without a bot token every message goes to the log sink
(outbox/), so the flow demos with no credentials.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import secrets
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from finpilot.config import get_settings
from finpilot.db.session import get_engine
from finpilot.delivery.log_sink import write_outbox
from finpilot.templating import render_snippet

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
OFFSET_KEY = "tg:offset"  # Redis: last processed update id (polling mode)


def _hmac(value: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def webhook_secret() -> str:
    """Path secret for the webhook URL (derived, so it never needs storing)."""
    return _hmac("telegram-webhook")[:32]


def _token() -> str | None:
    t = get_settings().telegram_bot_token
    return t.get_secret_value() if t else None


def send_telegram(chat_id: str, body: str, subject: str = "") -> str:
    """Returns the route used: 'telegram' or 'log_sink'."""
    tok = _token()
    if not tok:
        write_outbox("telegram", to=chat_id, subject=subject, body=body)
        return "log_sink"
    r = httpx.post(
        f"{API}/bot{tok}/sendMessage",
        json={"chat_id": chat_id, "text": body[:4000], "disable_web_page_preview": True},
        timeout=10,
    )
    r.raise_for_status()
    return "telegram"


def create_link(s: Session, user_id: uuid.UUID) -> dict[str, Any]:
    st = get_settings()
    token = secrets.token_urlsafe(16)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=st.telegram_link_ttl_min)
    s.execute(
        text(
            """INSERT INTO telegram_link_tokens (user_id, token_hash, expires_at) VALUES (:u, :h, :e)"""
        ),
        {"u": user_id, "h": _hmac(token), "e": expires},
    )
    return {
        "token": token,
        "url": f"https://t.me/{st.telegram_bot_username}?start={token}"
        if st.telegram_bot_username
        else None,
        "expires_at": expires.isoformat(),
        "bot_configured": bool(_token()),
    }


def bind(token: str, chat_id: str) -> uuid.UUID | None:
    """Consume a link token (pre-auth path → SECURITY DEFINER function)."""
    with get_engine().begin() as c:
        uid = c.execute(
            text("SELECT telegram_bind(:h, :c)"), {"h": _hmac(token.strip()), "c": chat_id}
        ).scalar()
    return uuid.UUID(str(uid)) if uid else None


def handle_update(update: dict[str, Any]) -> str:
    """One Bot API update. Only "/start <token>" does anything; everything else gets help text."""
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    body = str(msg.get("text") or "")
    if chat is None:
        return "ignored"
    chat_id = str(chat)
    if body.startswith("/start ") and len(body) > 7:
        uid = bind(body[7:], chat_id)
        send_telegram(
            chat_id, render_snippet("digest", "telegram.linked" if uid else "telegram.bad_token")
        )
        return "linked" if uid else "bad_token"
    send_telegram(chat_id, render_snippet("digest", "telegram.help"))
    return "help"


def poll_once(redis: Any) -> int:
    """Polling mode: fetch pending updates after the stored offset and handle them."""
    tok = _token()
    if not tok:
        return 0
    offset = int(redis.get(OFFSET_KEY) or 0)
    r = httpx.get(
        f"{API}/bot{tok}/getUpdates", params={"offset": offset + 1, "timeout": 0}, timeout=15
    )
    r.raise_for_status()
    n = 0
    for upd in r.json().get("result", []):
        try:
            handle_update(upd)
        finally:
            redis.set(OFFSET_KEY, int(upd["update_id"]))
        n += 1
    return n
