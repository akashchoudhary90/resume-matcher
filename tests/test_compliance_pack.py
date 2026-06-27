"""Compliance Pack: the bias audit as a signed, tamper-evident, audit-ready deliverable."""
import pytest

from resume_matcher.audit.compliance_pack import build_compliance_pack, verify_compliance_pack
from resume_matcher.audit.defense_file import issuer_public_key


def _fake_audit():
    return {
        "available": True, "n_selected": 40, "n_pool": 120,
        "attributes": {
            "race_ethnicity": {"four_fifths_pass": False, "min_impact_ratio": 0.64, "flagged": True,
                               "groups": [], "notes": []},
            "gender": {"four_fifths_pass": True, "min_impact_ratio": 0.91, "flagged": False,
                       "groups": [], "notes": []},
        },
        "exposure": {"parity_ratio": 0.74},
        "proxy_leakage": {"computable": True, "auc": 0.55, "leakage": False},
    }


def test_build_and_verify_roundtrip():
    pack = build_compliance_pack(_fake_audit(), generated_at=1000.0)
    assert pack["format"] == "resume-matcher-compliance-pack"
    assert pack["sig_alg"] == "ed25519" and pack["public_key"]
    v = verify_compliance_pack(pack, expected_public_key=issuer_public_key()["public_key"])
    assert v["ok"] and v["content_intact"] and v["signature_valid"] and v["issuer_verified"]


def test_honest_positioning_present():
    pack = build_compliance_pack(_fake_audit(), generated_at=1000.0)
    blob = (pack["disclaimer"] + pack["methodology"]).lower()
    assert "not an independent" in blob and "local law 144" in blob.lower() or "ll144" in blob
    assert pack["score_kind"] == "fit_readiness_not_hire_probability"
    assert pack["audit"]["attributes"]["race_ethnicity"]["min_impact_ratio"] == 0.64  # carries the real numbers


def test_tampering_with_a_number_is_detected():
    pack = build_compliance_pack(_fake_audit(), generated_at=1000.0)
    pack["audit"]["attributes"]["race_ethnicity"]["min_impact_ratio"] = 0.99   # forge a passing ratio
    v = verify_compliance_pack(pack)
    assert v["content_intact"] is False and v["ok"] is False


def test_forged_signature_detected():
    pack = build_compliance_pack(_fake_audit(), generated_at=1000.0)
    pack["signature"] = "00" * 64
    assert verify_compliance_pack(pack)["signature_valid"] is False


def test_endpoint_returns_signed_pack_after_load():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("scipy")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    client = TestClient(create_app())
    client.post("/api/load-synthetic")              # loads synthetic data + self-ID
    r = client.get("/api/compliance-pack.json")
    if r.status_code == 400:
        pytest.skip("synthetic self-ID not available in this environment")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    pack = r.json()
    assert verify_compliance_pack(pack, expected_public_key=issuer_public_key()["public_key"])["ok"]
