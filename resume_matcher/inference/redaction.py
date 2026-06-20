"""PII redaction — the single chokepoint that strips direct identifiers from resume text before it
reaches the scorer and, critically, before any NON-LOCAL adapter could ever see it (boundary #3).

This removes direct identifiers (email, phone, URLs, street address, and a supplied name). It is a
guardrail, not a guarantee of anonymity — free text can still leak identity. It deliberately does
NOT try to scrub ethnicity/community signals: those belong only in the separate audit store and are
never fed to scoring, so there is nothing here to "fix" by guessing at protected attributes.
"""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"(\+?\d[\d\-\s().]{7,}\d)")
URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
# North-American-ish street address line (number + street + suffix)
ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+([A-Z][a-z]+\s){1,3}(Street|St|Avenue|Ave|Road|Rd|Blvd|Boulevard|Lane|Ln|Drive|Dr|Court|Ct|Way)\b",
    re.IGNORECASE,
)
POSTAL_CA_RE = re.compile(r"\b[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d\b")  # Canadian postal code (a proxy)


def redact_text(text: str, name: str | None = None) -> str:
    """Return `text` with direct identifiers replaced by typed placeholders."""
    if not text:
        return ""
    out = EMAIL_RE.sub("[EMAIL]", text)
    out = URL_RE.sub("[URL]", out)
    out = ADDRESS_RE.sub("[ADDRESS]", out)
    out = POSTAL_CA_RE.sub("[POSTAL]", out)
    out = PHONE_RE.sub("[PHONE]", out)
    if name:
        for part in {p for p in re.split(r"\s+", name.strip()) if len(p) > 1}:
            out = re.sub(rf"\b{re.escape(part)}\b", "[NAME]", out)
    return out


def assert_redacted(text: str) -> list[str]:
    """Return a list of leak descriptions still present in `text` (empty == clean). Used by tests
    and as a tripwire before any non-local adapter call."""
    leaks: list[str] = []
    if EMAIL_RE.search(text):
        leaks.append("email")
    if PHONE_RE.search(text):
        leaks.append("phone")
    if URL_RE.search(text):
        leaks.append("url")
    if ADDRESS_RE.search(text):
        leaks.append("address")
    return leaks
