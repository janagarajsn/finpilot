"""Development delivery sink: writes outgoing messages to `outbox/` and the log."""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from pathlib import Path

from finpilot.config import ROOT_DIR

log = logging.getLogger(__name__)
OUTBOX = ROOT_DIR / "outbox"


def write_outbox(
    channel: str, *, to: str, subject: str, body: str, outbox: Path | None = None
) -> Path:
    d = outbox or OUTBOX
    d.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    path = d / f"{stamp}-{channel}-{secrets.token_hex(3)}.json"
    path.write_text(
        json.dumps({"channel": channel, "to": to, "subject": subject, "body": body}, indent=2)
    )
    log.info("[log_sink] %s to %s: %s → %s", channel, to, subject, path.name)
    return path
