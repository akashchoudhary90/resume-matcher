"""The public Independent Verifier: re-check a Defense File without trusting (or contacting) the issuer."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from resume_matcher.api.demo import SessionStore, run_demo
from resume_matcher.audit.defense_file import build_defense_file, issuer_public_key, verify_defense_file

ROOT = Path(__file__).resolve().parents[1]


def _session():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(store=store, required_skills=["python", "sql", "docker"],
                    files=[("Alice.txt", b"Python and SQL developer. Built REST APIs. Bachelor. " * 4)])
    return sess.to_dict()


def test_issuer_check_passes_for_our_own_file():
    f = build_defense_file(_session(), generated_at=1000.0)
    v = verify_defense_file(f, expected_public_key=issuer_public_key()["public_key"])
    assert v["ok"] and v["issuer_verified"] is True


def test_forger_with_own_key_fails_issuer_check(monkeypatch):
    # A forger signs a SELF-CONSISTENT file with their OWN key. Chain + signatures pass, but the issuer
    # authenticity check (against OUR published key) fails -> ok False. This is the whole point of the
    # verifier: the public key embedded in a file alone proves nothing.
    our_key = issuer_public_key()["public_key"]
    monkeypatch.setenv("RM_DEFENSE_SIGNING_SEED", "ff" * 32)        # the forger's different key
    forged = build_defense_file(_session(), generated_at=1000.0)
    assert forged["public_key"] != our_key
    v = verify_defense_file(forged, expected_public_key=our_key)
    assert v["signatures_valid"] is True                            # internally self-consistent
    assert v["issuer_verified"] is False and v["ok"] is False       # but not from us


def _client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    return TestClient(create_app())


def _make_file_via_api(client):
    files = [("resumes", ("Alice.txt", io.BytesIO(b"Python and SQL developer. Bachelor. " * 4), "text/plain"))]
    sid = client.post("/api/demo/run", data={"required_skills": "python;sql"}, files=files).json()["session_id"]
    return client.get(f"/api/demo/session/{sid}/defense-file.json").json()


def test_pubkey_endpoint_is_public():
    c = _client()
    r = c.get("/api/defense-file/pubkey")
    assert r.status_code == 200 and r.json()["sig_alg"] == "ed25519" and r.json()["public_key"]


def test_public_verify_roundtrip_and_tamper():
    c = _client()
    f = _make_file_via_api(c)
    assert c.post("/api/verify", json=f).json()["ok"] is True       # authentic -> verified
    f["records"][0]["fit_score"] = 99.9                             # forge a score
    assert c.post("/api/verify", json=f).json()["ok"] is False


def test_public_verify_rejects_non_defense_file():
    assert _client().post("/api/verify", json={"hello": "world"}).status_code == 400


def test_verifier_surfaces_are_auth_exempt(monkeypatch):
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "s3cret")
    c = _client()
    assert c.get("/api/status").status_code == 401                 # gated routes still require sign-in
    assert c.get("/verify").status_code == 200                     # but the verifier is PUBLIC
    assert c.get("/api/defense-file/pubkey").status_code == 200
    assert c.post("/api/verify", json={"records": []}).status_code == 200


def test_standalone_cli_verifier(tmp_path):
    f = build_defense_file(_session(), generated_at=1000.0)
    path = tmp_path / "defense-file.json"
    path.write_text(json.dumps(f), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "scripts/verify_defense_file.py", str(path),
         "--issuer-key", issuer_public_key()["public_key"]],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stderr
    assert '"ok": true' in r.stdout
