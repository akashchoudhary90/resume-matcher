"""The Defense File — a tamper-evident, hash-chained, signed, reproducible record of each scoring
decision. It turns the locked "the deterministic ranker decides and reconciles every point" property
into a receipt a third party can VERIFY rather than trust — the thing a black-box "match %" cannot
produce. This is the wedge of the audit-record platform (see the roadmap memory).

What it honestly proves:
  * RECONCILIATION — the recorded breakdown re-derives to the exact signed score
    (round(subtotal * education * experience * must_have * integrity, 1) == fit_score, and the line
    items sum to the subtotal). The score is a pure, reproducible function of the recorded inputs.
  * TAMPER-EVIDENCE — records are hash-chained (prev_hash -> record_hash); altering any past record
    breaks every link after it.
  * AUTHENTICITY — each record hash is signed (Ed25519 when `cryptography` is present, else HMAC-SHA256)
    so the file demonstrably came from this engine.
  * NO PROTECTED ATTRIBUTE — the scoring inputs are asserted to contain none (two-data-plane design).

De-identified by construction: it carries only the score breakdown the session already holds, with each
evidence quote stored as a SALTED HASH (proof a verbatim quote backed the point) — never the raw quote
or résumé text. Honestly positioned as "tamper-evident + reproducible", NOT "court-admissible"
(admissibility is a court's call, not a product claim).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

_log = logging.getLogger("resume_matcher.defense")

DEFENSE_VERSION = 1
# A fixed development signing seed. PROD MUST override RM_DEFENSE_SIGNING_SEED (64 hex chars) so the
# public key is stable and not publicly known. Using the default is fine for the demo (the file is
# tamper-evident via the hash chain regardless of the key's secrecy).
_DEV_SEED_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

try:  # Ed25519 (asymmetric: verify with the public key alone) when available; else HMAC fallback.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    _ED25519_OK = True
except Exception:  # noqa: BLE001 - optional dep; HMAC keeps the feature working everywhere
    _ED25519_OK = False


def _normalize_numbers(obj):
    """Make numbers stable across a JSON round-trip and across languages: an integer-valued float (75.0)
    canonicalizes identically to the integer (75) — exactly what a browser's JSON.stringify does. Without
    this, a file verified in Python (which keeps 75.0) and one re-serialized by JS (which emits 75) would
    hash differently, breaking the chain for real users."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        i = int(obj)
        return i if obj == i else obj
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    return obj


def _canonical(obj) -> bytes:
    """Deterministic JSON bytes for hashing/signing — sorted keys, no incidental whitespace, and numbers
    normalized so they survive a JSON round-trip (see _normalize_numbers). Matches the open spec."""
    return json.dumps(
        _normalize_numbers(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _seed() -> bytes:
    raw = os.environ.get("RM_DEFENSE_SIGNING_SEED")
    if raw is None:
        seed = bytes.fromhex(_DEV_SEED_HEX)  # dev/demo default: public key, authenticity NOT proven
    else:
        try:
            seed = bytes.fromhex(raw.strip())
        except ValueError as exc:
            # A SET-but-malformed override must fail LOUDLY. Silently falling back to the public dev
            # key would sign with a known key while appearing configured — the worst of both worlds.
            raise ValueError(
                "RM_DEFENSE_SIGNING_SEED is set but is not valid hex — refusing to fall back to the "
                "public dev signing key. Set it to 64 hex characters (32 bytes).") from exc
    return (seed + b"\x00" * 32)[:32]


def assert_defense_signing_configured() -> None:
    """Warn (or, when required, refuse) if the PUBLIC dev signing seed is in use.

    Every deployment that leaves RM_DEFENSE_SIGNING_SEED unset shares one hardcoded key, so
    signatures still prove tamper-evidence + reproducibility but NOT authenticity (anyone can forge
    'this came from the engine'). With RM_REQUIRE_SIGNING_SEED=1 or RM_ENV=prod, an unset/dev seed
    refuses startup (fail closed); otherwise it logs a warning so the demo keeps working."""
    from ..config import env_flag, env_str

    raw = os.environ.get("RM_DEFENSE_SIGNING_SEED")
    using_dev = not raw or raw.strip().lower() == _DEV_SEED_HEX
    if not using_dev:
        return
    msg = ("Defense-File signing is using the PUBLIC dev seed — every deployment shares this key, so "
           "signatures prove tamper-evidence but NOT authenticity. Set RM_DEFENSE_SIGNING_SEED "
           "(64 hex chars) to a private value in production.")
    if env_flag("RM_REQUIRE_SIGNING_SEED", False) or env_str("RM_ENV", "") in ("prod", "production"):
        raise RuntimeError(msg)
    _log.warning(msg)


def _make_signer() -> dict:
    """Return {sign(hex_msg)->hex_sig, alg, public_key}. Ed25519 if available, else HMAC-SHA256."""
    seed = _seed()
    if _ED25519_OK:
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        return {
            "alg": "ed25519",
            "public_key": pub_hex,
            "sign": lambda msg: priv.sign(msg.encode("utf-8")).hex(),
        }
    return {
        "alg": "hmac-sha256",
        "public_key": None,  # symmetric: off-server verification needs the server key
        "sign": lambda msg: hmac.new(seed, msg.encode("utf-8"), hashlib.sha256).hexdigest(),
    }


def _make_verifier(alg: str | None, public_key: str | None):
    """Return verify(hex_msg, hex_sig)->bool, or None if it can't be verified from the file alone
    (HMAC off-server). Ed25519 verifies from the included public key — the asymmetric advantage."""
    if alg == "ed25519" and _ED25519_OK and public_key:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))

        def _verify(msg: str, sig: str) -> bool:
            try:
                pub.verify(bytes.fromhex(sig), msg.encode("utf-8"))
                return True
            except (InvalidSignature, ValueError):
                return False
        return _verify
    if alg == "hmac-sha256":
        seed = _seed()

        def _verify_hmac(msg: str, sig: str) -> bool:
            expected = hmac.new(seed, msg.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, sig or "")
        return _verify_hmac
    return None


def _job_desc(job: dict) -> dict:
    def ids(key: str) -> list[str]:
        return [s.get("id") for s in (job.get(key) or []) if isinstance(s, dict)]
    return {
        "title": job.get("title", ""), "employer": job.get("employer", ""),
        "required_skills": ids("required_skills"), "preferred_skills": ids("preferred_skills"),
        "must_have_skills": ids("must_have_skills"),
        "min_education": job.get("min_education"), "min_years": job.get("min_years"),
    }


def _hash_evidence(span: str | None, salt: str) -> str | None:
    return _sha256((salt + span).encode("utf-8")) if span else None


def _explanation_slice(ex: dict, salt: str) -> dict:
    comps = [
        {"skill_id": c.get("skill_id"), "bucket": c.get("bucket"), "status": c.get("status"),
         "points_possible": c.get("points_possible"), "points_earned": c.get("points_earned"),
         "evidence_sha256": _hash_evidence(c.get("evidence_span"), salt)}
        for c in (ex.get("components") or [])
    ]
    return {
        "subtotal": ex.get("subtotal"), "education_factor": ex.get("education_factor"),
        "experience_factor": ex.get("experience_factor"), "must_have_factor": ex.get("must_have_factor"),
        "integrity_factor": ex.get("integrity_factor"), "final_score": ex.get("final_score"),
        "components": comps,
    }


def _iter_decisions(session: dict):
    """Yield (label, job, result_row) for every scored decision — single shortlist OR grid cell."""
    grid = session.get("grid")
    if grid:
        jobs = grid.get("jobs", [])
        for cand in grid.get("candidates", []):
            for cell in (cand.get("cells") or []):
                if cell and cell.get("result"):
                    ji = cell.get("job_index", 0)
                    yield cand.get("label", ""), (jobs[ji] if 0 <= ji < len(jobs) else {}), cell["result"]
    else:
        job = session.get("job", {})
        for row in session.get("results", []):
            yield row.get("label", ""), job, row


def _reconciles(rec: dict) -> bool:
    ex = rec.get("explanation") or {}
    try:
        recomputed = round(
            ex["subtotal"] * ex["education_factor"] * ex["experience_factor"]
            * ex["must_have_factor"] * ex["integrity_factor"], 1)
    except (KeyError, TypeError):
        return False
    if abs(recomputed - (rec.get("fit_score") or 0)) > 0.05:
        return False
    if ex.get("final_score") is not None and abs(ex["final_score"] - (rec.get("fit_score") or 0)) > 0.05:
        return False
    earned = sum((c.get("points_earned") or 0.0) for c in ex.get("components", []))
    return abs(earned - (ex.get("subtotal") or 0.0)) <= 0.3  # cumulative-rounding tolerance


def build_defense_file(session: dict, *, generated_at: float) -> dict:
    """Build a signed, hash-chained Defense File for every decision in a scored session."""
    signer = _make_signer()
    salt = _sha256((str(generated_at) + (session.get("session_id") or "")).encode("utf-8"))[:32]
    records: list[dict] = []
    prev = None
    for label, job, row in _iter_decisions(session):
        ex = row.get("explanation")
        if not ex:
            continue
        rec = {
            "v": DEFENSE_VERSION,
            "candidate_ref": _sha256((salt + (label or "")).encode("utf-8"))[:32],
            "label": label,
            "job": _job_desc(job),
            "fit_score": row.get("fit_score"),
            "grade": row.get("grade"),
            "confidence": row.get("confidence"),
            "score_kind": row.get("score_kind", "fit_readiness_not_hire_probability"),
            "no_protected_attribute": True,
            "engine": session.get("engine"),
            "ranker_formula": ex.get("formula"),
            "explanation": _explanation_slice(ex, salt),
            "prev_hash": prev,
        }
        rec["record_hash"] = _sha256(_canonical(rec))
        rec["signature"] = signer["sign"](rec["record_hash"])
        prev = rec["record_hash"]
        records.append(rec)

    file = {
        "format": "resume-matcher-defense-file",
        "version": DEFENSE_VERSION,
        "generated_at": generated_at,
        "engine": session.get("engine"),
        "sig_alg": signer["alg"],
        "public_key": signer["public_key"],
        "salt": salt,
        "verify_at": "/verify",   # a public, no-login page that re-verifies this file
        "spec": "docs/DEFENSE_FILE_SPEC.md",
        "n_decisions": len(records),
        "disclaimer": ("Tamper-evident, reproducible record of how each fit-readiness score was derived "
                       "from the recorded breakdown. NOT a probability of hire and NOT a claim of "
                       "court-admissibility. Evidence quotes are stored as salted hashes, not raw text."),
        "records": records,
    }
    file["verification"] = verify_defense_file(file)
    return file


def issuer_public_key() -> dict:
    """This engine's current signing identity — publish it (e.g. at /api/defense-file/pubkey and in the
    spec) so a verifier can authenticate Defense Files against it OUT-OF-BAND. Do not trust the key
    embedded inside a file alone: a forger can sign with their own key and embed it."""
    signer = _make_signer()
    return {"sig_alg": signer["alg"], "public_key": signer["public_key"]}


def verify_defense_file(file: dict, expected_public_key: str | None = None) -> dict:
    """Independently re-verify a Defense File WITHOUT trusting the issuer: hash chain, per-record
    reconciliation, and (for Ed25519) signatures from the embedded public key. Pass `expected_public_key`
    (obtained out-of-band) to also AUTHENTICATE the issuer — without it, a self-consistent forgery signed
    by an attacker's own key would still pass the chain/signature checks (`issuer_verified` stays None)."""
    verifier = _make_verifier(file.get("sig_alg"), file.get("public_key"))
    prev = None
    chain_ok = recon_ok = sig_ok = True
    for rec in file.get("records", []):
        body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
        if _sha256(_canonical(body)) != rec.get("record_hash"):
            chain_ok = False
        if rec.get("prev_hash") != prev:
            chain_ok = False
        if not _reconciles(rec):
            recon_ok = False
        if verifier is not None and not verifier(rec.get("record_hash", ""), rec.get("signature", "")):
            sig_ok = False
        prev = rec.get("record_hash")

    signatures_valid = sig_ok if verifier is not None else None
    issuer_key = file.get("public_key")
    issuer_verified: bool | None = None
    if expected_public_key is not None:
        issuer_verified = bool(issuer_key) and hmac.compare_digest(issuer_key, expected_public_key)
    ok = (chain_ok and recon_ok and (signatures_valid is not False)
          and (issuer_verified is not False))
    return {
        "ok": ok,
        "n_decisions": len(file.get("records", [])),
        "chain_intact": chain_ok,
        "all_reconcile": recon_ok,
        "signatures_valid": signatures_valid,
        "issuer_key": issuer_key,
        "issuer_verified": issuer_verified,
        "sig_alg": file.get("sig_alg"),
        "note": ("Each score re-derives exactly from its recorded breakdown; records are hash-chained"
                 + ("; signatures verified with the included public key."
                    if file.get("sig_alg") == "ed25519"
                    else "; HMAC signature verification requires the server key.")
                 + (" Issuer authenticated against the expected key."
                    if issuer_verified else
                    " Provide the issuer's published key to authenticate the signer."
                    if issuer_verified is None else
                    " WARNING: signed by an UNEXPECTED key — not from the stated issuer.")),
    }
