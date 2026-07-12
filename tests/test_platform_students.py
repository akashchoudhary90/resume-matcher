"""Phase-2 slices I–M: student profile/consents/resume, applications, the live matching loop
(shortlists, roles-for-you, exposure events), and notifications."""
from __future__ import annotations

import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher import notify  # noqa: E402
from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402
from resume_matcher.stores.students import StudentStore  # noqa: E402

JD = """Python Developer Intern

Requirements
- Strong Python skills are required
- SQL experience preferred
"""

RESUME = ("Alex Candidate\nalex@example.com | 416-555-0100\n"
          "Skills: Python, SQL, Git. Built REST APIs with Python and SQL for two years.\n") * 2


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    monkeypatch.setenv("RM_INFERENCE_BACKEND", "mock")  # deterministic engine for the match loop
    accounts = get_account_store()
    tokens = {}
    tokens["employer"], _ = accounts.register("hr@acme.com", "password123",
                                              role="employer", org_name="Acme Corp")
    tokens["student"], _ = accounts.register("stu@york.ca", "password123")
    tokens["coordinator"], _ = accounts.create_user("coord@york.ca", "password123",
                                                    role="coordinator")
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    client = TestClient(create_app())
    return client, tokens, org_id


def _as(client, tokens, role):
    client.cookies.set("rm_session", tokens[role])
    return client


def _wait_jobs_done(timeout: float = 20.0):
    """Wait until the worker pool drains the queue (jobs are enqueued by routes under test)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(connect()) as conn:
            busy = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if not busy:
            return
        time.sleep(0.05)
    raise AssertionError("jobs did not drain")


def _make_live_posting(client, tokens, org_id) -> str:
    _as(client, tokens, "coordinator")
    assert client.post(f"/api/coordinator/org-links/{org_id}/approve").status_code == 200
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={
        "fields": {"title": "Python Developer Intern", "description": JD},
        "skills": [{"skill_id": "python", "bucket": "required"},
                   {"skill_id": "sql", "bucket": "preferred"}],
    }).json()["posting_id"]
    assert client.post(f"/api/postings/{pid}/submit").status_code == 200
    _as(client, tokens, "coordinator")
    assert client.post(f"/api/coordinator/postings/{pid}/approve").status_code == 200
    return pid


def _student_ready(client, tokens):
    _as(client, tokens, "student")
    client.put("/api/students/me/profile", json={"program": "CS", "grad_year": 2027})
    for purpose in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
    r = client.post("/api/students/me/resume",
                    files={"resume": ("alex.txt", RESUME.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()


# ---- Slice I -------------------------------------------------------------------------------------
def test_resume_upload_requires_consent_and_redacts(platform):
    client, tokens, _ = platform
    _as(client, tokens, "student")
    r = client.post("/api/students/me/resume",
                    files={"resume": ("alex.txt", RESUME.encode(), "text/plain")})
    assert r.status_code == 409  # no resume_storage consent yet

    meta = _student_ready(client, tokens)
    assert meta["skills_detected"] >= 2
    # the stored matching text is redacted (no direct identifiers)
    store = StudentStore()
    student_id = get_account_store().user_for_token(tokens["student"])["id"]
    pool = store.matchable_students()
    row = next(r for r in pool if r["user_id"] == student_id)
    assert "alex@example.com" not in row["redacted_text"]
    assert "416-555" not in row["redacted_text"]


def test_hard_delete_and_consent_gate_the_pool(platform):
    client, tokens, _ = platform
    _student_ready(client, tokens)
    student_id = get_account_store().user_for_token(tokens["student"])["id"]
    store = StudentStore()
    assert any(r["user_id"] == student_id for r in store.matchable_students())

    # revoking profile_matching removes the student from the pool
    client.post("/api/students/me/consents", json={"purpose": "profile_matching",
                                                   "granted": False})
    assert not any(r["user_id"] == student_id for r in store.matchable_students())
    client.post("/api/students/me/consents", json={"purpose": "profile_matching",
                                                   "granted": True})

    # hard delete removes the row (blob and text) entirely
    assert client.delete("/api/students/me/resume").json()["deleted"] is True
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resumes WHERE user_id=?",
                            (student_id,)).fetchone()[0] == 0


# ---- Slices J + K + L ------------------------------------------------------------------------------
def test_apply_and_matching_loop_end_to_end(platform):
    client, tokens, org_id = platform
    pid = _make_live_posting(client, tokens, org_id)
    _student_ready(client, tokens)          # upload enqueues rematch_student
    _wait_jobs_done()                       # match_posting (from approve) + rematch both drain

    # student sees "roles for you" with an honest, explained score
    _as(client, tokens, "student")
    matches = client.get("/api/students/me/matches").json()
    assert matches["score_kind"] == "fit_readiness_not_hire_probability"
    top = matches["matches"][0]
    assert top["posting_id"] == pid and top["fit_score"] > 0
    assert top["explanation"] and top["explanation"]["components"]      # Slice L: the why

    # apply (once)
    assert client.post(f"/api/postings/{pid}/apply").status_code == 201
    assert client.post(f"/api/postings/{pid}/apply").status_code == 409  # dupe
    mine = client.get("/api/students/me/applications").json()["applications"]
    assert mine[0]["posting_id"] == pid and mine[0]["status"] == "applied"

    # employer shortlist: ranked, joined with the application, exposure event recorded once
    _as(client, tokens, "employer")
    shortlist = client.get(f"/api/postings/{pid}/shortlist").json()["shortlist"]
    assert shortlist and shortlist[0]["application_status"] == "applied"
    assert shortlist[0]["candidate_ref"].startswith("student-")
    client.get(f"/api/postings/{pid}/shortlist")  # second view: no second event
    employer_id = get_account_store().user_for_token(tokens["employer"])["id"]
    with closing(connect()) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE actor_user_id=? AND action='shortlist_exposed' "
            "AND entity_id=?", (employer_id, pid)).fetchone()[0]
    assert n == 1

    # employer advances the application; student can request human review
    app_id = shortlist[0]["application_id"]
    assert client.patch(f"/api/applications/{app_id}",
                        json={"status": "shortlisted"}).json()["status"] == "shortlisted"
    assert client.patch(f"/api/applications/{app_id}",
                        json={"status": "applied"}).status_code == 409   # no going backwards
    _as(client, tokens, "student")
    assert client.post(f"/api/applications/{app_id}/request-human-review").json()["ok"] is True

    # applicant list shows the flag; email only behind the contact consent
    _as(client, tokens, "employer")
    apps = client.get(f"/api/postings/{pid}/applications").json()["applications"]
    assert apps[0]["human_review_requested"] == 1 and apps[0]["email"] is None
    _as(client, tokens, "student")
    client.post("/api/students/me/consents", json={"purpose": "contact", "granted": True})
    _as(client, tokens, "employer")
    assert client.get(f"/api/postings/{pid}/applications").json()["applications"][0]["email"] \
        == "stu@york.ca"


def test_resume_upload_without_saved_profile_still_joins_pool(platform):
    """Regression (found live): a student who never clicked Save Profile must not be silently
    excluded from matching — upload creates the default visible profile row."""
    client, tokens, _ = platform
    _as(client, tokens, "student")
    for purpose in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
    r = client.post("/api/students/me/resume",
                    files={"resume": ("alex.txt", RESUME.encode(), "text/plain")})
    assert r.status_code == 201
    student_id = get_account_store().user_for_token(tokens["student"])["id"]
    assert any(row["user_id"] == student_id for row in StudentStore().matchable_students())


def test_apply_rules(platform):
    client, tokens, org_id = platform
    pid = _make_live_posting(client, tokens, org_id)
    _as(client, tokens, "student")
    assert client.post(f"/api/postings/{pid}/apply").status_code == 409   # no resume yet
    _student_ready(client, tokens)
    _as(client, tokens, "employer")
    client.post(f"/api/postings/{pid}/close")
    _as(client, tokens, "student")
    assert client.post(f"/api/postings/{pid}/apply").status_code == 404   # not live anymore


# ---- Slice M ---------------------------------------------------------------------------------------
def test_notifications_fire_when_configured(platform, monkeypatch):
    client, tokens, org_id = platform
    sent = []
    monkeypatch.setattr(notify, "send", lambda to, subject, body: sent.append((to, subject)))
    pid = _make_live_posting(client, tokens, org_id)   # approve -> "Your posting is live"
    assert any(to == "hr@acme.com" and "live" in subj for to, subj in sent)
    _student_ready(client, tokens)
    _wait_jobs_done()
    _as(client, tokens, "student")
    client.post(f"/api/postings/{pid}/apply")           # -> "New application"
    assert any("application" in subj.lower() for _, subj in sent)


def test_notify_noop_without_smtp(monkeypatch):
    monkeypatch.delenv("RM_SMTP_HOST", raising=False)
    assert notify.send("x@y.z", "s", "b") is False      # silent no-op, no exception
