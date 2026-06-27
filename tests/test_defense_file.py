"""The Defense File: signed, hash-chained, reproducible decision records that anyone can re-verify."""
import io
import json

import pytest

from resume_matcher.api.demo import SessionStore, run_demo
from resume_matcher.audit import defense_file as df
from resume_matcher.audit.defense_file import build_defense_file, verify_defense_file


def _session():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, job_text="Python developer with SQL.",
        required_skills=["python", "sql", "docker"],
        files=[("Alice.txt", b"Python and SQL developer. Built REST APIs. Bachelor of Science. " * 4),
               ("Bob.txt", b"Java developer. Some Python. Master of Engineering. " * 4)],
    )
    return sess.to_dict()


def test_build_and_verify_roundtrip():
    f = build_defense_file(_session(), generated_at=1000.0)
    assert f["n_decisions"] == 2
    assert f["sig_alg"] == "ed25519" and f["public_key"]          # cryptography present in the test env
    v = verify_defense_file(f)
    assert v["ok"] and v["chain_intact"] and v["all_reconcile"] and v["signatures_valid"] is True
    # de-identified: only salted evidence hashes, never the raw quote or résumé text
    blob = json.dumps(f)
    assert "evidence_span" not in blob and "evidence_sha256" in blob
    for rec in f["records"]:
        assert rec["no_protected_attribute"] is True
        assert rec["score_kind"] == "fit_readiness_not_hire_probability"


def test_self_verification_block_is_present_and_passes():
    f = build_defense_file(_session(), generated_at=1000.0)
    assert f["verification"]["ok"] is True
    assert f["verification"]["n_decisions"] == 2


def test_tampering_with_a_score_is_detected():
    f = build_defense_file(_session(), generated_at=1000.0)
    f["records"][0]["fit_score"] = 99.9               # forge a higher score, don't fix the hash
    v = verify_defense_file(f)
    assert v["ok"] is False                            # hash mismatch AND reconciliation both fail
    assert v["chain_intact"] is False or v["all_reconcile"] is False


def test_tampering_with_the_chain_is_detected():
    f = build_defense_file(_session(), generated_at=1000.0)
    f["records"][0]["label"] = "Someone Else"          # alter a field without updating record_hash
    assert verify_defense_file(f)["chain_intact"] is False


def test_forged_signature_is_detected():
    f = build_defense_file(_session(), generated_at=1000.0)
    f["records"][0]["signature"] = "00" * 64           # can't forge Ed25519 without the private key
    v = verify_defense_file(f)
    assert v["signatures_valid"] is False and v["ok"] is False


def test_internally_consistent_forgery_still_fails_on_signature():
    # A sophisticated forger fixes the hash to match a forged record — but still can't produce a valid
    # Ed25519 signature. The signature is the backstop.
    f = build_defense_file(_session(), generated_at=1000.0)
    rec = f["records"][0]
    rec["grade"] = "A"
    body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
    rec["record_hash"] = df._sha256(df._canonical(body))   # re-hash to look consistent
    # signature was over the OLD hash -> no longer valid for the new hash
    assert verify_defense_file(f)["signatures_valid"] is False


def test_hmac_fallback_when_cryptography_absent(monkeypatch):
    monkeypatch.setattr(df, "_ED25519_OK", False)
    f = build_defense_file(_session(), generated_at=1000.0)
    assert f["sig_alg"] == "hmac-sha256" and f["public_key"] is None
    v = verify_defense_file(f)
    assert v["ok"] and v["chain_intact"] and v["all_reconcile"] and v["signatures_valid"] is True


def test_defense_file_endpoint():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    client = TestClient(create_app())
    files = [("resumes", ("Alice.txt", io.BytesIO(b"Python and SQL developer. Bachelor. " * 4), "text/plain"))]
    sid = client.post("/api/demo/run", data={"required_skills": "python;sql"}, files=files).json()["session_id"]
    r = client.get(f"/api/demo/session/{sid}/defense-file.json")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    f = r.json()
    assert f["verification"]["ok"] is True and f["n_decisions"] == 1
