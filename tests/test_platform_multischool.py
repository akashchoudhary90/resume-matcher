"""Slice V: the multi-school marketplace — hard school isolation for postings, queues, and the
match pool; per-school employer approval links."""
from __future__ import annotations

import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402

RESUME = ("Sam.\nSkills: Python, SQL. Built REST APIs with Python and SQL for two years.\n") * 2


@pytest.fixture()
def two_schools(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    monkeypatch.setenv("RM_INFERENCE_BACKEND", "mock")
    accounts = get_account_store()
    with closing(connect()) as conn:  # school 1 = seeded York; add school 2
        conn.execute("INSERT INTO schools(id, name, created_at) VALUES(2, 'Seneca', ?)",
                     (time.time(),))
        conn.commit()
    tokens = {}
    tokens["employer"], _ = accounts.register("hr@acme.com", "password123",
                                              role="employer", org_name="Acme Corp")  # york
    tokens["studentA"], _ = accounts.register("a@york.ca", "password123")             # york
    tokens["studentB"], _ = accounts.register("b@seneca.ca", "password123", school_id=2)
    tokens["coordA"], _ = accounts.create_user("ca@york.ca", "password123", role="coordinator")
    tokens["coordB"], _ = accounts.create_user("cb@seneca.ca", "password123",
                                               role="coordinator", school_id=2)
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    return TestClient(create_app()), tokens, org_id


def _as(client, tokens, who):
    client.cookies.set("rm_session", tokens[who])
    return client


def _wait_jobs_done(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(connect()) as conn:
            if not conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN "
                                "('queued','running')").fetchone()[0]:
                return
        time.sleep(0.05)
    raise AssertionError("jobs did not drain")


def _go_live(client, tokens, approver, title, school_body=None) -> str:
    _as(client, tokens, "employer")
    body = {"fields": {"title": title, "description": "Python and SQL."},
            "skills": [{"skill_id": "python", "bucket": "required"}]}
    if school_body:
        body["school_id"] = school_body
    pid = client.post("/api/postings", json=body).json()["posting_id"]
    assert client.post(f"/api/postings/{pid}/submit").status_code == 200
    _as(client, tokens, approver)
    assert client.post(f"/api/coordinator/postings/{pid}/approve").status_code == 200
    return pid


def test_school_isolation_end_to_end(two_schools):
    client, tokens, org_id = two_schools

    # York coordinator approves Acme at York; a York-only posting goes live
    _as(client, tokens, "coordA")
    assert client.post(f"/api/coordinator/org-links/{org_id}/approve").status_code == 200
    pid_york = _go_live(client, tokens, "coordA", "York Dev Intern")

    # the Seneca student sees NOTHING from York
    _as(client, tokens, "studentB")
    assert client.get("/api/postings").json()["postings"] == []
    assert client.get(f"/api/postings/{pid_york}").status_code == 404
    assert client.post(f"/api/postings/{pid_york}/apply").status_code == 404

    # the York student sees it
    _as(client, tokens, "studentA")
    assert [p["id"] for p in client.get("/api/postings").json()["postings"]] == [pid_york]

    # posting to Seneca is blocked until Seneca's coordinator approves Acme THERE
    _as(client, tokens, "employer")
    pid_sen = client.post("/api/postings", json={
        "fields": {"title": "Seneca Dev Intern", "description": "Python."},
        "skills": [{"skill_id": "python", "bucket": "required"}],
        "school_id": 2}).json()["posting_id"]
    assert client.post(f"/api/postings/{pid_sen}/submit").status_code == 409  # no Seneca link

    r = client.post("/api/orgs/me/school-links", json={"school_id": 2})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    _as(client, tokens, "coordB")
    queue = client.get("/api/coordinator/queue").json()
    assert any(link["org_id"] == org_id for link in queue["org_links"])   # in SENECA's queue
    assert client.post(f"/api/coordinator/org-links/{org_id}/approve").status_code == 200

    _as(client, tokens, "employer")
    assert client.post(f"/api/postings/{pid_sen}/submit").status_code == 200

    # per-school review queues: Seneca sees the Seneca posting, York doesn't
    _as(client, tokens, "coordB")
    assert [p["id"] for p in client.get("/api/coordinator/queue").json()["postings"]] == [pid_sen]
    _as(client, tokens, "coordA")
    assert client.get("/api/coordinator/queue").json()["postings"] == []
    _as(client, tokens, "coordB")
    client.post(f"/api/coordinator/postings/{pid_sen}/approve")

    # the match pool is per school: only the Seneca student gets scored on the Seneca posting
    for who in ("studentA", "studentB"):
        _as(client, tokens, who)
        for purpose in ("resume_storage", "profile_matching"):
            client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
        client.post("/api/students/me/resume",
                    files={"resume": ("s.txt", RESUME.encode(), "text/plain")})
    _wait_jobs_done()
    _as(client, tokens, "studentB")
    b_matches = {m["posting_id"] for m in client.get("/api/students/me/matches").json()["matches"]}
    assert b_matches == {pid_sen}
    _as(client, tokens, "studentA")
    a_matches = {m["posting_id"] for m in client.get("/api/students/me/matches").json()["matches"]}
    assert a_matches == {pid_york}

    # the employer sees BOTH their postings across schools
    _as(client, tokens, "employer")
    mine = {p["id"] for p in client.get("/api/postings").json()["postings"]}
    assert mine == {pid_york, pid_sen}


def test_schools_api(two_schools):
    client, tokens, _ = two_schools
    names = {s["name"] for s in client.get("/api/schools").json()["schools"]}  # public
    assert {"York University", "Seneca"} <= names
    _as(client, tokens, "coordA")
    assert client.post("/api/schools", json={"name": "X"}).status_code == 403  # admin-only
    assert client.post("/api/orgs/me/school-links", json={"school_id": 2}).status_code == 403


def test_register_rejects_unknown_school(two_schools):
    client, _, _ = two_schools
    r = client.post("/api/account/register", json={
        "email": "x@nowhere.ca", "password": "password123", "school_id": 99})
    assert r.status_code == 400
