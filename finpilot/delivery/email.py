"""Email delivery: SMTP when configured, otherwise the log sink (outbox/ files) so every flow
demos without credentials."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from finpilot.config import get_settings
from finpilot.delivery.log_sink import write_outbox

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> str:
    """Returns the channel used: 'smtp' or 'log_sink'."""
    s = get_settings()
    if not s.smtp_host:
        write_outbox("email", to=to, subject=subject, body=body)
        return "log_sink"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if s.smtp_user and s.smtp_password:
            smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
        smtp.send_message(msg)
    return "smtp"
