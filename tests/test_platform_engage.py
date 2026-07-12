"""Phase-3 slices R/S/T: events & career fairs, application-thread messaging, interviews."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402

RESUME = ("Alex.\nSkills: Python, SQL. Built REST APIs with Python and SQL for two years.\n") * 2


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    monkeypatch.setenv("RM_INFERENCE_BACKEND", "mock")
    accounts = get_account_store()
    tokens = {}
    tokens["employer"], _ = accounts.register("hr@acme.com", "password123",
                                              role="employer", org_name="Acme Corp")
    tokens["rival"], _ = accounts.register("hr@rival.com", "password123",
                                           role="employer", org_name="Rival Inc")
    tokens["student"], _ = accounts.register("stu@york.ca", "password123")
    tokens["coordinator"], _ = accounts.create_user("coord@york.ca", "password123",
                                                    role="coordinator")
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    client = TestClient(create_app())
    return client, tokens, org_id


def _as(client, tokens, role):
    client.cookies.set("rm_session", tokens[role])
    return client


def _live_application(client, tokens, org_id) -> tuple[str, str]:
    """live posting + one application; returns (posting_id, application_id)."""
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={
        "fields": {"title": "Dev Intern", "description": "Python and SQL."},
        "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/postings/{pid}/approve")
    _as(client, tokens, "student")
    for purpose in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("alex.txt", RESUME.encode(), "text/plain")})
    app_id = client.post(f"/api/postings/{pid}/apply").json()["application_id"]
    return pid, app_id


# ---- Slice R -------------------------------------------------------------------------------------
def test_event_lifecycle_and_registration(platform):
    client, tokens, _ = platform
    _as(client, tokens, "student")
    assert client.post("/api/events", json={"title": "X"}).status_code == 403  # coordinator-only

    _as(client, tokens, "coordinator")
    eid = client.post("/api/events", json={
        "title": "Winter Career Fair", "kind": "fair", "location": "Vari Hall",
        "starts_at": time.time() + 86400}).json()["event_id"]

    _as(client, tokens, "student")
    assert client.get("/api/events").json()["events"] == []          # drafts invisible
    assert client.post(f"/api/events/{eid}/register").status_code == 409  # not published

    _as(client, tokens, "coordinator")
    assert client.patch(f"/api/events/{eid}",
                        json={"status": "published"}).json()["status"] == "published"

    _as(client, tokens, "student")
    events = client.get("/api/events").json()["events"]
    assert events[0]["title"] == "Winter Career Fair" and events[0]["registered"] is False
    assert client.post(f"/api/events/{eid}/register").status_code == 200
    assert client.post(f"/api/events/{eid}/register").status_code == 409   # dupe
    assert client.get("/api/events").json()["events"][0]["registered"] is True
    assert client.get(f"/api/events/{eid}/attendees").status_code == 403   # not for students

    _as(client, tokens, "employer")
    client.post(f"/api/events/{eid}/register")                              # booth
    _as(client, tokens, "coordinator")
    attendees = client.get(f"/api/events/{eid}/attendees").json()["attendees"]
    assert {a["role"] for a in attendees} == {"student", "employer"}
    assert any(a["org_name"] == "Acme Corp" for a in attendees)

    # unregister + cancelled events stop registration
    _as(client, tokens, "student")
    client.post(f"/api/events/{eid}/unregister")
    _as(client, tokens, "coordinator")
    client.patch(f"/api/events/{eid}", json={"status": "cancelled"})
    _as(client, tokens, "student")
    assert client.post(f"/api/events/{eid}/register").status_code == 409


# ---- Slice S -------------------------------------------------------------------------------------
def test_messaging_is_application_scoped(platform):
    client, tokens, org_id = platform
    _, app_id = _live_application(client, tokens, org_id)

    _as(client, tokens, "employer")
    r = client.post(f"/api/applications/{app_id}/messages",
                    json={"body": "Thanks for applying — quick question about your SQL work?"})
    assert r.status_code == 201

    # the rival employer can't even see the thread exists
    _as(client, tokens, "rival")
    assert client.get(f"/api/applications/{app_id}/messages").status_code == 404

    # the student reads (marks read) and replies
    _as(client, tokens, "student")
    assert client.get("/api/messages/unread-count").json()["unread"] == 1
    thread = client.get(f"/api/applications/{app_id}/messages").json()["messages"]
    assert thread[0]["sender_role"] == "employer"
    assert client.get("/api/messages/unread-count").json()["unread"] == 0
    client.post(f"/api/applications/{app_id}/messages", json={"body": "Happy to elaborate!"})

    _as(client, tokens, "employer")
    assert client.get("/api/messages/unread-count").json()["unread"] == 1
    thread = client.get(f"/api/applications/{app_id}/messages").json()["messages"]
    assert len(thread) == 2 and thread[1]["sender_role"] == "student"

    # empty body rejected
    assert client.post(f"/api/applications/{app_id}/messages",
                       json={"body": "  "}).status_code == 400


# ---- Slice T -------------------------------------------------------------------------------------
def test_interview_slots_accept_declines_siblings(platform):
    client, tokens, org_id = platform
    _, app_id = _live_application(client, tokens, org_id)
    base = time.time() + 7 * 86400

    _as(client, tokens, "student")
    assert client.post(f"/api/applications/{app_id}/interview-slots",
                       json={"slots": []}).status_code == 403   # students don't propose

    _as(client, tokens, "employer")
    slots = client.post(f"/api/applications/{app_id}/interview-slots", json={"slots": [
        {"starts_at": base, "ends_at": base + 1800},
        {"starts_at": base + 3600, "ends_at": base + 5400},
        {"starts_at": base + 7200, "ends_at": base + 9000},
    ]}).json()["slots"]
    assert len(slots) == 3

    _as(client, tokens, "student")
    upcoming = client.get("/api/students/me/interviews").json()["interviews"]
    assert len(upcoming) == 3 and upcoming[0]["title"] == "Dev Intern"
    accepted = client.post(f"/api/interview-slots/{slots[1]['id']}/accept").json()
    assert accepted["status"] == "accepted"
    statuses = {s["id"]: s["status"] for s in
                client.get(f"/api/applications/{app_id}/interview-slots").json()["slots"]}
    assert statuses[slots[0]["id"]] == "declined"
    assert statuses[slots[2]["id"]] == "declined"
    # can't accept a declined sibling
    assert client.post(f"/api/interview-slots/{slots[0]['id']}/accept").status_code == 409

    # employer cancels the accepted one
    _as(client, tokens, "employer")
    assert client.post(f"/api/interview-slots/{slots[1]['id']}/cancel").json()["status"] \
        == "cancelled"

    # rival employer sees nothing
    _as(client, tokens, "rival")
    assert client.get(f"/api/applications/{app_id}/interview-slots").status_code == 404
