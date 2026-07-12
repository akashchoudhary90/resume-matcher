"""Platform API (api/platform.py): the full Phase-1 lifecycle — employer extract → create →
org-link gate → submit → coordinator queue → approve (WFWA disclosure) → student sees it live —
plus role denials and the corrections→eval writer."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.platform import AI_DISCLOSURE  # noqa: E402

JD = """Data Analyst Intern

Requirements
- Strong Python skills are required
- 2+ years with SQL

Compensation
$24 - $28 per hour

How to Apply
Apply by 2030-03-15 at https://jobs.example.com/apply
"""


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")  # deterministic draft only
    monkeypatch.setenv("RM_JD_CORRECTIONS_PATH", str(tmp_path / "corrections.jsonl"))
    accounts = get_account_store()
    tokens = {}
    tokens["employer"], _ = accounts.register("hr@acme.com", "password123",
                                              role="employer", org_name="Acme Corp")
    tokens["student"], _ = accounts.register("stu@york.ca", "password123")
    tokens["coordinator"], _ = accounts.create_user("coord@york.ca", "password123",
                                                    role="coordinator")
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    client = TestClient(create_app())
    return client, tokens, org_id, tmp_path


def _as(client, tokens, role):
    client.cookies.set("rm_session", tokens[role])
    return client


def _poll_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_full_phase1_lifecycle(platform):
    client, tokens, org_id, tmp_path = platform

    # --- employer extracts a pasted JD (202 + poll) ---
    _as(client, tokens, "employer")
    r = client.post("/api/postings/extract", json={"job_text": JD, "title": ""})
    assert r.status_code == 202
    job = _poll_job(client, r.json()["job_id"])
    assert job["status"] == "done"
    draft = job["result"]["draft"]
    assert draft["pay"]["value"]["min"] == 24.0
    skill_ids = {s["skill_id"] for s in draft["skills"]}
    assert {"python", "sql"} <= skill_ids

    # --- employer creates the posting from the reviewed draft (edits the title) ---
    fields = {"title": "Data Analyst Intern (Summer)", "description": JD,
              "employment_type": "internship", "pay_min": 24, "pay_max": 28,
              "pay_currency": "CAD", "pay_period": "hour", "apply_deadline": "2030-03-15"}
    skills = [{"skill_id": "python", "bucket": "required"},
              {"skill_id": "sql", "bucket": "must_have"}]
    r = client.post("/api/postings", json={"fields": fields, "skills": skills,
                                           "extraction": {"draft": draft}})
    assert r.status_code == 201
    pid = r.json()["posting_id"]

    # corrections were recorded (title changed, sql bucket changed)
    lines = [json.loads(line) for line in
             (tmp_path / "corrections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(rec["field"] == "title" and rec["changed"] for rec in lines)
    assert any(rec["field"] == "skills[sql].bucket" for rec in lines)

    # --- submit blocked until the coordinator approves the org link (the trust gate) ---
    assert client.post(f"/api/postings/{pid}/submit").status_code == 409
    _as(client, tokens, "coordinator")
    assert client.post(f"/api/coordinator/org-links/{org_id}/approve").status_code == 200
    _as(client, tokens, "employer")
    r = client.post(f"/api/postings/{pid}/submit")
    assert r.status_code == 200 and r.json()["status"] == "pending_review"

    # --- coordinator queue shows it; approval goes live and appends the WFWA disclosure ---
    _as(client, tokens, "coordinator")
    queue = client.get("/api/coordinator/queue").json()
    assert any(p["id"] == pid for p in queue["postings"])
    r = client.post(f"/api/coordinator/postings/{pid}/approve", json={"note": "ok"})
    assert r.status_code == 200
    live = r.json()
    assert live["status"] == "live" and live["ai_disclosure"] == 1
    assert AI_DISCLOSURE in live["description"]
    # append-only event log recorded every transition
    events = [(e["from_status"], e["to_status"]) for e in
              client.get(f"/api/postings/{pid}").json()["events"]]
    assert (None, "draft") in events and ("pending_review", "live") in events

    # --- student sees exactly the live posting, without extraction internals ---
    _as(client, tokens, "student")
    listing = client.get("/api/postings").json()["postings"]
    assert [p["id"] for p in listing] == [pid]
    body = client.get(f"/api/postings/{pid}").json()
    assert body["title"] == "Data Analyst Intern (Summer)" and body.get("extraction") is None

    # --- close ---
    _as(client, tokens, "employer")
    assert client.post(f"/api/postings/{pid}/close").json()["status"] == "closed"
    _as(client, tokens, "student")
    assert client.get(f"/api/postings/{pid}").status_code == 404  # closed -> not student-visible


def test_role_denials(platform):
    client, tokens, org_id, _ = platform

    _as(client, tokens, "student")
    assert client.post("/api/postings/extract", json={"job_text": JD}).status_code == 403
    assert client.post("/api/postings", json={"fields": {"title": "X"}}).status_code == 403
    assert client.get("/api/coordinator/queue").status_code == 403

    _as(client, tokens, "employer")
    assert client.get("/api/coordinator/queue").status_code == 403
    assert client.post(f"/api/coordinator/org-links/{org_id}/approve").status_code == 403

    client.cookies.clear()
    assert client.get("/api/postings").status_code == 401


def test_employer_cannot_touch_other_orgs_posting(platform):
    client, tokens, org_id, _ = platform
    accounts = get_account_store()
    tokens["rival"], _ = accounts.register("hr@rival.com", "password123",
                                           role="employer", org_name="Rival Inc")
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={"fields": {"title": "Ours"}}).json()["posting_id"]
    _as(client, tokens, "rival")
    assert client.get(f"/api/postings/{pid}").status_code == 404       # invisible cross-org
    assert client.patch(f"/api/postings/{pid}", json={"fields": {"title": "Hax"}}).status_code == 403
    assert client.post(f"/api/postings/{pid}/submit").status_code == 403


def test_skills_typeahead_requires_signin(platform):
    client, tokens, _, _ = platform
    client.cookies.clear()
    assert client.get("/api/skills?q=py").status_code == 401
    _as(client, tokens, "student")
    names = {s["name"].lower() for s in client.get("/api/skills?q=python").json()["skills"]}
    assert any("python" in n for n in names)


def test_extract_validates_input(platform):
    client, tokens, _, _ = platform
    _as(client, tokens, "employer")
    assert client.post("/api/postings/extract", json={}).status_code == 400
    # unreadable text becomes a job-level error with a client-facing message
    r = client.post("/api/postings/extract", json={"job_text": "   "})
    assert r.status_code == 400
