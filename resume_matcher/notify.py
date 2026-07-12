"""Email notifications (docs/IMPLEMENTATION.md Slice M) — stdlib smtplib, deliberately boring.

Configured by RM_SMTP_HOST (+ RM_SMTP_PORT, RM_SMTP_FROM); when unset every send() is a logged
no-op, so dev/tests/deployments-without-mail never break on a missing mail server. Sends are
best-effort: a mail failure must never fail the action that triggered it (callers already wrap).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import env_int, env_str

_log = logging.getLogger("resume_matcher.notify")


def configured() -> bool:
    return bool(env_str("RM_SMTP_HOST", ""))


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. Returns True when actually sent."""
    if not to:
        return False
    if not configured():
        _log.info("email skipped (RM_SMTP_HOST unset): to=%s subject=%r", to, subject)
        return False
    msg = EmailMessage()
    msg["From"] = env_str("RM_SMTP_FROM", "careers@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(env_str("RM_SMTP_HOST", ""), env_int("RM_SMTP_PORT", 25),
                          timeout=10) as smtp:
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001 - notifications are best-effort by contract
        _log.warning("email send failed: to=%s subject=%r", to, subject, exc_info=True)
        return False
