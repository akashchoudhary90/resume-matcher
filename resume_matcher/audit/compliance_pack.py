"""Compliance Pack — the bias audit as a signed, regulator-formatted, audit-ready deliverable.

The dashboard already computes the full bias audit (audit/metrics.py: four-fifths impact ratio per
protected attribute, exposure parity, homophily disparity, proxy leakage). This wraps that SAME output
in a tamper-evident, signed envelope (reusing the Defense File's hash + Ed25519 signing) so it can be
exported as a continuous, self-verifying monitoring artifact computed on the very engine that made the
decisions — something a black-box vendor's after-the-fact PDF cannot honestly claim.

HONEST POSITIONING (non-negotiable, per the reality review): this is CONTINUOUS INTERNAL MONITORING +
AUDIT-READY EVIDENCE, not "your NYC LL144 audit, done" — LL144 mandates an INDEPENDENT auditor, and the
numbers here are over SYNTHETIC data. It is not legal advice.
"""
from __future__ import annotations

from .defense_file import _canonical, _make_signer, _make_verifier, _sha256

COMPLIANCE_VERSION = 1

_METHODOLOGY = (
    "Scores are produced by a deterministic ranker (the LLM only extracts evidence; it never decides "
    "the score). A protected attribute NEVER enters scoring (two separate data planes). Bias is "
    "detect-and-flag only. 'Selected' = appears on any per-job shortlist. Four-fifths impact ratio is "
    "each group's selection rate over the most-selected group's, with Fisher's exact significance and a "
    "min-cell-5 suppression; exposure parity is position-discounted; proxy leakage trains an auxiliary "
    "classifier to predict the protected attribute from the scoring features."
)
_DISCLAIMER = (
    "Continuous INTERNAL monitoring + audit-ready evidence, computed on the same engine that made the "
    "decisions. NOT an independent NYC Local Law 144 audit (which requires an INDEPENDENT auditor), NOT "
    "a probability of hire, and NOT legal advice. The figures here are over SYNTHETIC demonstration data."
)


def build_compliance_pack(audit: dict, *, generated_at: float) -> dict:
    """Wrap an audit() result into a signed, self-verifying compliance pack."""
    signer = _make_signer()
    body = {
        "format": "resume-matcher-compliance-pack",
        "version": COMPLIANCE_VERSION,
        "generated_at": generated_at,
        "standard_refs": [
            "NYC Local Law 144 (four-fifths impact-ratio bias audit)",
            "EEOC Uniform Guidelines on Employee Selection Procedures (adverse impact)",
            "EU AI Act (high-risk AI: post-market monitoring)",
        ],
        "score_kind": "fit_readiness_not_hire_probability",
        "methodology": _METHODOLOGY,
        "disclaimer": _DISCLAIMER,
        "audit": audit,
    }
    digest = _sha256(_canonical(body))
    return {
        **body,
        "sig_alg": signer["alg"],
        "public_key": signer["public_key"],
        "content_sha256": digest,
        "signature": signer["sign"](digest),
        "verify_at": "/verify",
    }


def verify_compliance_pack(pack: dict, expected_public_key: str | None = None) -> dict:
    """Re-verify a compliance pack: recompute the content hash and check the signature (and, with the
    issuer's out-of-band key, authenticity). Mirrors the Defense File verifier."""
    body = {k: v for k, v in pack.items()
            if k not in ("content_sha256", "signature", "sig_alg", "public_key", "verify_at")}
    recomputed = _sha256(_canonical(body))
    content_ok = recomputed == pack.get("content_sha256")
    verifier = _make_verifier(pack.get("sig_alg"), pack.get("public_key"))
    sig_ok = None
    if verifier is not None:
        sig_ok = bool(content_ok and verifier(pack.get("content_sha256", ""), pack.get("signature", "")))
    issuer_key = pack.get("public_key")
    issuer_verified: bool | None = None
    if expected_public_key is not None:
        import hmac
        issuer_verified = bool(issuer_key) and hmac.compare_digest(issuer_key, expected_public_key)
    ok = content_ok and (sig_ok is not False) and (issuer_verified is not False)
    return {
        "ok": ok,
        "content_intact": content_ok,
        "signature_valid": sig_ok,
        "issuer_key": issuer_key,
        "issuer_verified": issuer_verified,
        "sig_alg": pack.get("sig_alg"),
    }
