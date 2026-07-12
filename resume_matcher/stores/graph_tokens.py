"""Identity tokenizer for the relationship graph (docs/RELATIONSHIPS.md Slice AA).

A person's identity (name + company, or an email) is turned into an opaque, per-school, versioned
MAC token. Tokens let us INTERSECT a member's uploaded contacts against consenting members WITHOUT
storing anyone's name — and the per-school key means the same person yields different tokens across
schools (limits cross-tenant linkage).

Honesty (Boundary #4): name+company is low-entropy, so a token is PSEUDONYMOUS to whoever holds
the key, NOT anonymous. Therefore the key must never sit in application memory in production — the
MAC is computed by a KMS/HSM. `_kms_mac` is the seam: wire a real KMS there for prod. In dev
(RM_ENV=dev) an env-var pepper (RM_GRAPH_PEPPER) is permitted. With no key material the tokenizer
is DISABLED (fail-closed) so the importer cannot silently fall back to a weak hash.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata

from ..config import env_str

# Bumped when the canonicalization rules change (old tokens then mismatch by design).
CANON_VERSION = "c1"

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|corp|corp\.|co|co\.|company|gmbh|plc|sa|srl|pvt|"
    r"limited|incorporated|corporation)\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class TokenizerUnavailable(Exception):
    """No key material — the importer must fail closed, never fall back to a weak hash."""


def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_component(s: str) -> str:
    s = _strip_diacritics(unicodedata.normalize("NFKC", s or "")).casefold()
    return _NON_ALNUM_RE.sub("", s)


def canonical_identity(first: str = "", last: str = "", company: str = "",
                       email: str = "") -> str | None:
    """A stable canonical string for a person. Email (normalized) wins when present — it is far
    higher-entropy than a name. Otherwise firstname+lastname+company with suffixes/diacritics/case
    stripped. Returns None when there isn't enough to identify anyone."""
    email = (email or "").strip().casefold()
    if "@" in email and len(email) >= 5:
        return f"{CANON_VERSION}\x1femail\x1f{email}"
    f, la = _norm_component(first), _norm_component(last)
    comp = _NON_ALNUM_RE.sub("", _strip_diacritics(
        _COMPANY_SUFFIX_RE.sub("", (company or "").casefold())))
    if not (f and la):           # a first+last is the minimum; a lone name is too ambiguous
        return None
    return f"{CANON_VERSION}\x1fname\x1f{f}\x1f{la}\x1f{comp}"


def available() -> bool:
    """True when key material exists (KMS in prod, or the dev pepper under RM_ENV=dev)."""
    if env_str("RM_ENV", "") == "dev" and env_str("RM_GRAPH_PEPPER", ""):
        return True
    return bool(env_str("RM_GRAPH_KMS_KEY_ID", ""))  # a real deployment sets this


def _kms_mac(key_material: bytes, message: bytes) -> str:
    """The MAC seam. PROD: replace this body with a KMS/HSM GenerateMac call so the key never
    enters this process. DEV: HMAC-SHA256 with the env pepper. Same interface either way."""
    return hmac.new(key_material, message, hashlib.sha256).hexdigest()


def _dev_key(school_id: int) -> bytes:
    pepper = env_str("RM_GRAPH_PEPPER", "")
    # per-school divergence: mix the school id into the key so the same person tokenizes
    # differently across tenants even under one dev pepper.
    return hashlib.sha256(f"{pepper}\x1f{school_id}".encode("utf-8")).digest()


def key_version() -> str:
    """Identifies which key produced a token, so keys can rotate without a full re-import."""
    if env_str("RM_ENV", "") == "dev" and env_str("RM_GRAPH_PEPPER", ""):
        return "dev1"
    return env_str("RM_GRAPH_KMS_KEY_VERSION", "kms1")


def identity_token(school_id: int, *, first: str = "", last: str = "", company: str = "",
                   email: str = "") -> tuple[str, str] | None:
    """(token, key_version) for a person at a school, or None if unidentifiable. Raises
    TokenizerUnavailable when no key material is configured (fail-closed)."""
    if not available():
        raise TokenizerUnavailable(
            "No graph key material. Set RM_GRAPH_KMS_KEY_ID (prod) or RM_ENV=dev + "
            "RM_GRAPH_PEPPER (dev). The contacts importer is disabled without it.")
    canonical = canonical_identity(first=first, last=last, company=company, email=email)
    if canonical is None:
        return None
    if env_str("RM_ENV", "") == "dev" and env_str("RM_GRAPH_PEPPER", ""):
        key = _dev_key(school_id)
    else:  # prod: the KMS holds the key; we pass a per-school context, never a raw key
        key = f"{os.environ['RM_GRAPH_KMS_KEY_ID']}\x1f{school_id}".encode("utf-8")
    return _kms_mac(key, canonical.encode("utf-8")), key_version()
