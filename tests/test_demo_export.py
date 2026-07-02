"""Shortlist export (CSV/JSON) for the ephemeral demo — streamed from RAM, never written to disk."""
import csv
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("multipart")  # python-multipart, required for file uploads

from conftest import finish_demo_run  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def _file(name, body):
    return ("resumes", (name, io.BytesIO(body), "text/plain"))


def _make_session(client):
    r = finish_demo_run(client, client.post(
        "/api/demo/run",
        data={"job_text": "Python developer with SQL.", "title": "Backend Dev",
              "employer": "Acme", "required_skills": "python;sql"},
        files=[_file("alice.txt", b"Python and SQL developer. Built REST APIs. Bachelor. " * 4),
               _file("bob.txt", b"Java and Docker developer. Some Python. Master. " * 4)],
    ))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    return r.json()["session_id"]


def test_export_csv_is_ranked_and_honest(client):
    sid = _make_session(client)
    r = client.get(f"/api/demo/session/{sid}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["Rank", "Candidate", "Fit score"]   # honest header, not "match %"
    assert "match %" not in r.text.lower()
    assert len(rows) == 3                                        # header + 2 candidates
    assert rows[1][0] == "1" and rows[2][0] == "2"              # ranked, top-down
    assert "alice@" not in r.text                                # no contact PII leaks into the export


def test_export_json_carries_full_results(client):
    sid = _make_session(client)
    r = client.get(f"/api/demo/session/{sid}/export.json")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["explanation"] is not None        # full breakdown, not just the summary


def test_export_unknown_session_404(client):
    assert client.get("/api/demo/session/nope/export.csv").status_code == 404
    assert client.get("/api/demo/session/nope/export.json").status_code == 404


def test_export_gated_by_kill_switch(monkeypatch):
    monkeypatch.setenv("RM_DEMO_ENABLED", "0")
    gated = TestClient(create_app())
    assert gated.get("/api/demo/session/x/export.csv").status_code == 403
    assert gated.get("/api/demo/session/x/export.json").status_code == 403
