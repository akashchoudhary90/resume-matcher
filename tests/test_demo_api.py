"""HTTP surface for the ephemeral real-data demo. Skipped when FastAPI/httpx aren't installed."""
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")  # python-multipart, required for file uploads

from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def _file(name, body):
    return ("resumes", (name, io.BytesIO(body), "text/plain"))


def test_demo_config(client):
    cfg = client.get("/api/demo/config").json()
    assert cfg["enabled"] is True
    assert cfg["max_resumes"] >= 1
    assert ".pdf" in cfg["supported_exts"]


def test_parse_job_endpoint(client):
    r = client.post("/api/demo/parse-job", data={"job_text": "Python and SQL. Bachelor required.", "title": "Dev"})
    d = r.json()
    ids = {s["id"] for s in d["detected_skills"]}
    assert {"python", "sql"} <= ids
    assert d["min_education"] == "bachelor"


def test_run_results_and_delete(client, demo_finish):
    files = [
        _file("alice.txt", b"Alice alice@x.com\nPython and SQL expert. Built REST APIs. Bachelor. " * 4),
        _file("bob.txt", b"Bob\nJava and Docker and Python. React user. Master degree. " * 4),
    ]
    data = {
        "job_text": "Python developer with SQL and Docker.",
        "title": "Backend Dev", "employer": "Acme",
        "required_skills": "python;sql;docker", "preferred_skills": "react",
        "min_education": "bachelor",
    }
    accepted = client.post("/api/demo/run", data=data, files=files)
    assert accepted.status_code == 202, accepted.text  # async contract: accepted now, scored behind
    r = demo_finish(client, accepted)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    sid = body["session_id"]
    assert body["n_resumes"] == 2
    assert body["privacy"]["stored_on_disk"] is False
    assert "alice@x.com" not in r.text  # no PII echoed back

    # every result reconciles and carries evidence
    for res in body["results"]:
        ex = res["explanation"]
        assert round(ex["subtotal"] * ex["education_factor"], 1) == res["fit_score"]
        assert "flags_explained" in res

    # fetch again, then delete, then 404
    assert client.get(f"/api/demo/session/{sid}").status_code == 200
    assert client.delete(f"/api/demo/session/{sid}").json()["deleted"] is True
    assert client.get(f"/api/demo/session/{sid}").status_code == 404


def test_too_many_resumes_400(client):
    files = [_file(f"r{i}.txt", b"python sql") for i in range(11)]
    r = client.post("/api/demo/run", data={"required_skills": "python"}, files=files)
    assert r.status_code == 400
    # #19: a generic client message; the raw parser exception is logged server-side, not echoed.
    detail = r.json().get("detail", "")
    assert "Upload rejected" in detail and "Exception" not in detail and "Traceback" not in r.text


def test_demo_run_rate_limited(monkeypatch):
    # #1: a single client flooding /api/demo/run is throttled (429) after the burst is spent.
    monkeypatch.setenv("RM_DEMO_RATE_BURST", "2")
    monkeypatch.setenv("RM_DEMO_RATE_PER_MIN", "1")  # ~0.017/s refill — negligible during the test
    throttled = TestClient(create_app())
    files = [_file("a.txt", b"python developer")]
    codes = [throttled.post("/api/demo/run", data={"required_skills": "python"}, files=files).status_code
             for _ in range(3)]
    assert codes[:2] == [202, 202]  # accepted (async)
    assert codes[2] == 429  # burst of 2 exhausted -> third request rejected


def test_demo_page_served(client):
    assert client.get("/demo").status_code == 200


def test_large_resume_over_1mb_accepted(client, demo_finish):
    # Starlette's default 1 MB per-part limit would reject this; the demo raises it to the file cap.
    big = b"Python developer with SQL experience. " + b" word" * 250_000  # ~1.3 MB
    r = demo_finish(client, client.post(
        "/api/demo/run", data={"required_skills": "python;sql"},
        files=[("resumes", ("big.txt", io.BytesIO(big), "text/plain"))],
    ))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"


def test_uploads_configured_to_stay_in_memory():
    # The spool threshold must exceed the max file size so no upload part spills to a disk temp file.
    create_app()
    import starlette.formparsers as fp

    from resume_matcher.api import demo as demo_mod

    assert fp.MultiPartParser.spool_max_size >= demo_mod.MAX_FILE_MB * 1024 * 1024


def test_demo_disabled_returns_403(monkeypatch):
    monkeypatch.setenv("RM_DEMO_ENABLED", "0")
    gated = TestClient(create_app())
    files = [_file("a.txt", b"python")]
    assert gated.post("/api/demo/run", data={"required_skills": "python"}, files=files).status_code == 403
    assert gated.post("/api/demo/parse-job", data={"job_text": "python"}).status_code == 403
    # The kill switch must also gate session read/delete, not just create.
    assert gated.get("/api/demo/session/whatever").status_code == 403
    assert gated.delete("/api/demo/session/whatever").status_code == 403
