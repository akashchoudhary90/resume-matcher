"""Web API tests. Skipped automatically when FastAPI / httpx (TestClient) aren't installed, so the
core CI run (requirements.txt only) stays green; install requirements-extra.txt to exercise them."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def test_health_and_index(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200  # dashboard HTML served


def test_load_and_browse(client):
    status = client.post("/api/load-synthetic").json()
    assert status["loaded"] and status["n_jobs"] > 0
    assert status["score_kind"] == "fit_readiness_not_hire_probability"

    jobs = client.get("/api/jobs").json()
    assert jobs
    jid = jobs[0]["job_id"]

    sl = client.get(f"/api/jobs/{jid}/shortlist").json()
    assert sl["shortlist"] and 0 <= sl["shortlist"][0]["fit_score"] <= 100

    cid = client.get("/api/candidates").json()[0]
    cand = client.get(f"/api/candidates/{cid}").json()
    assert cand["candidate_id"] == cid and "closest_fit" in cand

    audit = client.get("/api/audit").json()
    assert audit["available"] and "race_ethnicity" in audit["attributes"]


def test_unknown_ids_404(client):
    client.post("/api/load-synthetic")
    assert client.get("/api/jobs/NOPE/shortlist").status_code == 404
    assert client.get("/api/candidates/NOPE").status_code == 404


def test_admin_password_gate(monkeypatch):
    # When RM_ADMIN_PASSWORD is set, every route requires Basic auth.
    monkeypatch.setenv("RM_ADMIN_USER", "admin")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "s3cret")
    gated = TestClient(create_app())

    assert gated.get("/api/health").status_code == 401            # no credentials
    assert gated.get("/api/health", auth=("admin", "wrong")).status_code == 401
    assert gated.get("/api/health", auth=("admin", "s3cret")).status_code == 200
    assert gated.get("/", auth=("admin", "s3cret")).status_code == 200  # dashboard gated too
