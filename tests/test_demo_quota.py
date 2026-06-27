"""Demo usage-quota gate: full functionality, limited quantity (RM_DEMO_FREE_RUNS).

A client may try every feature, but only on a few small batches per window; re-running the SAME batch
is free, and a spent client gets a 402 + upgrade prompt instead of more matches.
"""
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")  # python-multipart, required for file uploads

from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402


def _file(name, body):
    return ("resumes", (name, io.BytesIO(body), "text/plain"))


def _run(client, n):
    """A DISTINCT batch each call (the résumé bytes vary by n -> distinct content hash)."""
    return client.post(
        "/api/demo/run",
        data={"required_skills": "python", "job_text": "Python developer."},
        files=[_file(f"r{n}.txt", f"Python and SQL developer number {n}. ".encode() * 4)],
    )


def test_quota_blocks_after_free_runs(monkeypatch):
    monkeypatch.setenv("RM_DEMO_FREE_RUNS", "2")
    client = TestClient(create_app())
    assert _run(client, 1).status_code == 200
    assert _run(client, 2).status_code == 200
    r = _run(client, 3)
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["upgrade"] is True
    assert detail["remaining"] == 0
    # The honest framing: a taste-gate, not a billing wall — no PII / internals leaked.
    assert "alice@" not in r.text


def test_quota_rerun_same_batch_is_free(monkeypatch):
    monkeypatch.setenv("RM_DEMO_FREE_RUNS", "1")
    client = TestClient(create_app())
    assert _run(client, 1).status_code == 200          # spends the one free match
    assert _run(client, 1).status_code == 200          # SAME batch -> free re-score, not blocked
    assert _run(client, 2).status_code == 402          # a genuinely new batch is gated


def test_quota_charges_only_successful_runs(monkeypatch):
    # A failed run (no readable text) must NOT burn a match.
    monkeypatch.setenv("RM_DEMO_FREE_RUNS", "1")
    client = TestClient(create_app())
    bad = client.post("/api/demo/run", data={"required_skills": "python"},
                      files=[_file("empty.txt", b"   ")])
    assert bad.status_code == 400
    assert _run(client, 1).status_code == 200          # the one free match survived the failed attempt


def test_quota_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RM_DEMO_FREE_RUNS", raising=False)
    client = TestClient(create_app())
    for i in range(4):
        assert _run(client, i).status_code == 200      # unmetered
    cfg = client.get("/api/demo/config").json()
    assert cfg["free_runs"] == 0
    assert cfg["runs_remaining"] is None


def test_config_and_run_expose_remaining(monkeypatch):
    monkeypatch.setenv("RM_DEMO_FREE_RUNS", "3")
    client = TestClient(create_app())
    assert client.get("/api/demo/config").json()["runs_remaining"] == 3
    body = _run(client, 1).json()
    assert body["quota"] == {"limit": 3, "remaining": 2}
    assert client.get("/api/demo/config").json()["runs_remaining"] == 2
