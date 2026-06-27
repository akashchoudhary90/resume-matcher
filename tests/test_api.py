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
    h = client.get("/api/health")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "ok" and "active_sessions" in body  # real readiness probe
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
    assert "exposure" in audit  # #26: rank-aware exposure parity wired in
    if "homophily" in audit:
        assert "reference_basis" in audit["homophily"]  # #26: reference group made explicit


def test_unknown_ids_404(client):
    client.post("/api/load-synthetic")
    assert client.get("/api/jobs/NOPE/shortlist").status_code == 404
    assert client.get("/api/candidates/NOPE").status_code == 404


def test_admin_signin_gate(monkeypatch):
    # When RM_ADMIN_PASSWORD is set, routes require a session cookie obtained via the /login form.
    monkeypatch.setenv("RM_ADMIN_USER", "admin")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "s3cret")
    gated = TestClient(create_app())

    assert gated.get("/api/status").status_code == 401                 # no session -> API 401
    r = gated.get("/", follow_redirects=False)                         # page -> redirect to /login
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert gated.get("/login").status_code == 200                      # the sign-in page is reachable

    assert gated.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert gated.post("/api/login", json={"username": "admin", "password": "s3cret"}).status_code == 200
    # the TestClient now carries the session cookie -> gated routes work
    assert gated.get("/api/status").status_code == 200
    assert gated.get("/").status_code == 200
    gated.post("/api/logout")                                          # logout clears the cookie
    assert gated.get("/api/status").status_code == 401


def test_health_is_auth_exempt(monkeypatch):
    # /api/health must answer 200 WITHOUT a session even when the gate is on — the Docker HEALTHCHECK
    # and the auto-deploy poller hit it unauthenticated. It exposes only status + counts, no PII.
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "s3cret")
    gated = TestClient(create_app())
    r = gated.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("weak", ["admin", "password"])
def test_weak_admin_password_now_allowed_with_warning(monkeypatch, weak):
    # The demo is intentionally usable with admin/admin: a weak password warns but no longer refuses.
    monkeypatch.setenv("RM_ADMIN_USER", "admin")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", weak)
    gated = TestClient(create_app())                                   # must NOT raise
    assert gated.post("/api/login", json={"username": "admin", "password": weak}).status_code == 200


def test_unset_admin_password_starts_open(monkeypatch):
    # Unset is still allowed: local-dev open mode (warned per-request, not refused).
    monkeypatch.delenv("RM_ADMIN_PASSWORD", raising=False)
    assert TestClient(create_app()).get("/api/health").status_code == 200
