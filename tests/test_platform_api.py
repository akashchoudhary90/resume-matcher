"""Platform API (api/platform.py): the full Phase-1 lifecycle — employer extract → create →
org-link gate → submit → coordinator queue → approve (WFWA disclosure) → student sees it live —
plus role denials and the corrections→eval writer.

Phase-5 slice S5 adds the route-level enforcement to the same surfaces: the A1 repudiation queue
(202-shaped both ways, IP-limited, school-scoped decisions), B6 posting search, and B7 withdrawal.
"""
from __future__ import annotations

import json
import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402
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
    monkeypatch.setenv("RM_ENV", "dev")            # the repudiation queue tokenizes asserted names
    monkeypatch.setenv("RM_GRAPH_PEPPER", "test-pepper")
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


# ==================================================================================================
# Phase 5 / A1 — the public repudiation queue (docs/PHASE5.md §3.1)
# ==================================================================================================
def _reset_repudiate_limiter():
    """The limiter is a process-global token bucket; tests share the process."""
    from resume_matcher.api import platform as platform_mod

    platform_mod._repudiate_rate._buckets.clear()


def test_repudiate_is_a_queue_not_a_delete_button(platform):
    """A1. Both bodies answer 202 with a request id and NOTHING is deleted at this route: an
    anonymous assertion buys a challenge or a review, never a mutation."""
    client, tokens, _, _ = platform
    _reset_repudiate_limiter()
    client.cookies.clear()   # public: no sign-in

    r = client.post("/api/graph/repudiate", json={"email": "stranger@ext.com"})
    assert r.status_code == 202
    assert r.json()["status"] == "challenge_sent" and r.json()["request_id"]

    r = client.post("/api/graph/repudiate",
                    json={"first": "Jane", "last": "Doe", "company": "Acme"})
    assert r.status_code == 202
    assert r.json()["status"] == "queued_for_review" and r.json()["request_id"]

    with closing(connect()) as conn:
        rows = {r["kind"]: r["status"] for r in
                conn.execute("SELECT kind, status FROM repudiation_requests")}
        # the emailed token is NEVER at rest in cleartext, and never returned to the caller
        hashes = conn.execute("SELECT challenge_hash FROM repudiation_requests "
                              "WHERE kind='email_challenge'").fetchone()["challenge_hash"]
    assert rows == {"email_challenge": "pending", "name_review": "pending"}
    assert len(hashes) == 64                       # sha256 hex, not a token
    assert "email_token" not in r.json()


def test_repudiate_answers_the_same_shape_for_members_and_strangers(platform):
    """The no-oracle invariant: a member's address and a nonexistent one are indistinguishable."""
    client, tokens, _, _ = platform
    _reset_repudiate_limiter()
    client.cookies.clear()
    member = client.post("/api/graph/repudiate", json={"email": "stu@york.ca"})
    stranger = client.post("/api/graph/repudiate", json={"email": "nobody@nowhere.example"})
    assert member.status_code == stranger.status_code == 202
    assert set(member.json()) == set(stranger.json()) == {"status", "request_id"}
    assert member.json()["status"] == stranger.json()["status"] == "challenge_sent"


def test_repudiate_is_rate_limited_per_client(platform):
    """FL-L5: the limiter takes (key, now) — a one-arg call would TypeError on the hot path, and a
    public unauthenticated route is exactly where that must not happen."""
    client, tokens, _, _ = platform
    _reset_repudiate_limiter()
    client.cookies.clear()
    codes = [client.post("/api/graph/repudiate", json={"first": "A", "last": "B"},
                         headers={"X-Forwarded-For": "9.9.9.9"}).status_code for _ in range(5)]
    assert codes[:3] == [202, 202, 202] and codes[3:] == [429, 429]
    # a different client key has its own bucket
    assert client.post("/api/graph/repudiate", json={"first": "A", "last": "B"},
                       headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 202


def test_repudiate_confirm_executes_only_with_the_emailed_token(platform):
    client, tokens, _, _ = platform
    _reset_repudiate_limiter()
    client.cookies.clear()
    rid = client.post("/api/graph/repudiate",
                      json={"email": "stranger@ext.com"}).json()["request_id"]
    # the token only ever existed in the (unsent, RM_SMTP_HOST-less) mail — guessing fails
    assert client.post("/api/graph/repudiate/confirm",
                       json={"request_id": rid, "email": "stranger@ext.com",
                             "token": "guess"}).status_code == 400


def test_repudiation_queue_and_decision_are_school_scoped(platform):
    """SC-C1. The queue and the decision both derive school_id from the SESSION; another tenant's
    request is absent, so a cross-tenant decide is a 404 — not a 403 (which would confirm the id)."""
    client, tokens, _, _ = platform
    accounts = get_account_store()
    _reset_repudiate_limiter()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO schools(id, name, created_at) VALUES(2,'Other U',?)",
                     (time.time(),))
        conn.commit()
    tokens["coord2"], _ = accounts.create_user("c2@other.ca", "password123",
                                               role="coordinator", school_id=2)
    client.cookies.clear()
    rid = client.post("/api/graph/repudiate",
                      json={"school_id": 1, "first": "Jane", "last": "Doe",
                            "company": "Acme"}).json()["request_id"]

    # school 2's coordinator sees nothing and cannot decide school 1's request
    _as(client, tokens, "coord2")
    assert client.get("/api/coordinator/repudiations").json()["requests"] == []
    assert client.post(f"/api/coordinator/repudiations/{rid}/decide",
                       json={"approve": True}).status_code == 404

    # school 1's coordinator sees it, WITH the counts-only match preview (security L2)
    _as(client, tokens, "coordinator")
    queue = client.get("/api/coordinator/repudiations").json()["requests"]
    assert [q["id"] for q in queue] == [rid]
    assert queue[0]["member_matched"] is False and queue[0]["contact_matches"] == 0
    assert client.post(f"/api/coordinator/repudiations/{rid}/decide",
                       json={"approve": True}).json()["status"] == "approved"
    # the asserted third-party name is scrubbed at decision either way (privacy F6)
    with closing(connect()) as conn:
        row = conn.execute("SELECT first, last, company, status FROM repudiation_requests "
                           "WHERE id=?", (rid,)).fetchone()
    assert (row["first"], row["last"], row["company"]) == (None, None, None)
    assert row["status"] == "approved"


def test_repudiation_queue_needs_a_coordinator(platform):
    client, tokens, _, _ = platform
    _as(client, tokens, "student")
    assert client.get("/api/coordinator/repudiations").status_code == 403
    _as(client, tokens, "employer")
    assert client.post("/api/coordinator/repudiations/x/decide",
                       json={"approve": True}).status_code == 403


# ==================================================================================================
# Phase 5 / B6 — student posting search (docs/PHASE5.md §2.14, §3.1)
# ==================================================================================================
def _live_posting(client, tokens, org_id, fields):
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/org-links/{org_id}/approve")   # idempotent
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={"fields": fields}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/postings/{pid}/approve")
    return pid


@pytest.fixture()
def searchable(platform):
    client, tokens, org_id, _ = platform
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    ids = {}
    specs = [
        ("py", {"title": "Python Developer", "description": "Build APIs.",
                "employment_type": "internship", "work_mode": "remote", "pay_min": 30,
                "apply_deadline": "2030-06-01"}),
        ("sql", {"title": "SQL Analyst", "description": "Dashboards and Python scripts.",
                 "employment_type": "full_time", "work_mode": "onsite", "pay_min": 20,
                 "apply_deadline": "2030-01-15"}),
        ("design", {"title": "Design Intern", "description": "Figma work.",
                    "employment_type": "internship", "work_mode": "hybrid", "pay_min": 18}),
    ]
    for key, fields in specs:
        _as(client, tokens, "employer")
        pid = client.post("/api/postings", json={"fields": fields}).json()["posting_id"]
        client.post(f"/api/postings/{pid}/submit")
        _as(client, tokens, "coordinator")
        client.post(f"/api/coordinator/postings/{pid}/approve")
        ids[key] = pid
    return client, tokens, ids


def test_b6_search_filters_and_pages(searchable):
    client, tokens, ids = searchable
    _as(client, tokens, "student")

    body = client.get("/api/postings").json()
    assert set(body) == {"postings", "total", "page", "page_size"}
    assert body["total"] == 3 and body["page"] == 1

    # keyword hits title AND description AND org name
    assert {p["id"] for p in client.get("/api/postings?q=python").json()["postings"]} \
        == {ids["py"], ids["sql"]}                       # 'Python scripts' in the SQL description
    assert {p["id"] for p in client.get("/api/postings?q=acme").json()["postings"]} \
        == set(ids.values())                             # every posting is Acme Corp's

    # structured filters
    assert {p["id"] for p in client.get("/api/postings?employment_type=internship")
            .json()["postings"]} == {ids["py"], ids["design"]}
    assert {p["id"] for p in client.get("/api/postings?work_mode=remote").json()["postings"]} \
        == {ids["py"]}
    assert {p["id"] for p in client.get("/api/postings?pay_min=20").json()["postings"]} \
        == {ids["py"], ids["sql"]}
    assert {p["id"] for p in client.get("/api/postings?deadline_after=2030-02-01")
            .json()["postings"]} == {ids["py"]}          # the no-deadline posting is excluded too

    # pagination: total counts the whole match set, not the page
    page1 = client.get("/api/postings?page=1&page_size=2").json()
    page2 = client.get("/api/postings?page=2&page_size=2").json()
    assert len(page1["postings"]) == 2 and len(page2["postings"]) == 1
    assert page1["total"] == page2["total"] == 3
    assert not {p["id"] for p in page1["postings"]} & {p["id"] for p in page2["postings"]}
    # page_size is capped server-side (a client can't ask for the whole table)
    assert client.get("/api/postings?page_size=5000").json()["page_size"] == 50


def test_b6_sorts_are_whitelisted(searchable):
    client, tokens, ids = searchable
    _as(client, tokens, "student")
    by_pay = [p["id"] for p in client.get("/api/postings?sort=pay").json()["postings"]]
    assert by_pay == [ids["py"], ids["sql"], ids["design"]]
    by_deadline = [p["id"] for p in client.get("/api/postings?sort=deadline").json()["postings"]]
    assert by_deadline[:2] == [ids["sql"], ids["py"]]     # the null deadline sorts last
    assert by_deadline[2] == ids["design"]
    # an unknown sort falls back to 'newest' rather than reaching ORDER BY
    assert client.get("/api/postings?sort=pay);DROP TABLE postings--").status_code == 200
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 3


def test_b6_like_metacharacters_are_literal(searchable):
    """SL-L4: unescaped, '%' matches everything and '_' matches any character — the search box would
    be a wildcard scan over every posting in the school."""
    client, tokens, ids = searchable
    _as(client, tokens, "student")
    assert client.get("/api/postings?q=%25").json()["total"] == 0      # '%' finds nothing literally
    assert client.get("/api/postings?q=_").json()["total"] == 0
    assert client.get("/api/postings?q=P_thon").json()["total"] == 0   # '_' is not a joker
    assert client.get("/api/postings?q=%5C").json()["total"] == 0      # a lone backslash is literal
    assert client.get("/api/postings?q=Python").json()["total"] == 2   # ...and real terms still work


def test_b6_search_never_crosses_school_or_status(searchable):
    client, tokens, ids = searchable
    _as(client, tokens, "employer")
    draft = client.post("/api/postings", json={"fields": {"title": "Python Draft"}}) \
        .json()["posting_id"]
    with closing(connect()) as conn:      # a live posting at ANOTHER school
        conn.execute("INSERT INTO schools(id, name, created_at) VALUES(2,'Other U',?)",
                     (time.time(),))
        conn.execute("UPDATE postings SET school_id=2 WHERE id=?", (ids["design"],))
        conn.commit()
    _as(client, tokens, "student")
    found = {p["id"] for p in client.get("/api/postings?q=python").json()["postings"]}
    assert draft not in found              # drafts are not browsable
    assert ids["design"] not in found      # and neither is another school's live posting
    assert client.get("/api/postings").json()["total"] == 2


def test_employer_and_coordinator_lists_keep_the_old_shape(searchable):
    """B6 changed the STUDENT branch only — the review surfaces are unpaged by design."""
    client, tokens, ids = searchable
    _as(client, tokens, "employer")
    assert set(client.get("/api/postings").json()) == {"postings"}
    _as(client, tokens, "coordinator")
    assert set(client.get("/api/postings").json()) == {"postings"}


# ==================================================================================================
# Phase 5 / B7 — student-initiated withdrawal (docs/PHASE5.md §3.1)
# ==================================================================================================
RESUME = "Alex. Python and SQL. Built REST APIs with Python and SQL for two years. " * 4


def _applied(client, tokens, org_id):
    pid = _live_posting(client, tokens, org_id, {"title": "Dev", "description": "Python."})
    _as(client, tokens, "student")
    for p in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": p, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", RESUME.encode(), "text/plain")})
    app_id = client.post(f"/api/postings/{pid}/apply").json()["application_id"]
    return pid, app_id


def test_b7_student_withdraws_own_application(platform):
    client, tokens, org_id, _ = platform
    pid, app_id = _applied(client, tokens, org_id)
    _as(client, tokens, "student")
    assert client.post(f"/api/applications/{app_id}/withdraw").json() == {"status": "withdrawn"}
    # terminal: nothing leaves 'withdrawn', including a second withdraw
    assert client.post(f"/api/applications/{app_id}/withdraw").status_code == 409
    # the employer's applicant list drops it by default
    _as(client, tokens, "employer")
    assert client.get(f"/api/postings/{pid}/applications").json()["applications"] == []
    # ...and no reason text was ever collected anywhere
    with closing(connect()) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(applications)")]
    assert "withdraw_reason" not in cols and "reason" not in cols


def test_b7_withdraw_is_ownership_checked(platform):
    client, tokens, org_id, _ = platform
    accounts = get_account_store()
    tokens["other"], _ = accounts.register("other@york.ca", "password123")
    pid, app_id = _applied(client, tokens, org_id)
    _as(client, tokens, "other")
    assert client.post(f"/api/applications/{app_id}/withdraw").status_code == 404  # IDOR-shaped
    _as(client, tokens, "employer")
    assert client.post(f"/api/applications/{app_id}/withdraw").status_code == 403  # role gate
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM applications WHERE id=?",
                            (app_id,)).fetchone()["status"] == "applied"


def test_b7_employer_cannot_withdraw_for_a_student(platform):
    """The employer-side guard: withdrawal is the student's speech about their own candidacy."""
    client, tokens, org_id, _ = platform
    pid, app_id = _applied(client, tokens, org_id)
    _as(client, tokens, "employer")
    r = client.patch(f"/api/applications/{app_id}", json={"status": "withdrawn"})
    assert r.status_code == 409 and "withdraw their own" in r.json()["detail"]
    # a real employer transition still works
    assert client.patch(f"/api/applications/{app_id}",
                        json={"status": "shortlisted"}).json()["status"] == "shortlisted"
