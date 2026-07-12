"""Slice W: the persistent audit plane (separate file), voluntary self-ID, min-cell suppression,
and the coordinator funnel/EEO reports."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.audit_store import AuditDB  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402

RESUME = ("S.\nSkills: Python, SQL. Built REST APIs with Python and SQL for two years.\n") * 2


@pytest.fixture(autouse=True)
def _audit_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_AUDIT_DB", str(tmp_path / "audit.db"))


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    monkeypatch.setenv("RM_INFERENCE_BACKEND", "mock")
    accounts = get_account_store()
    tokens = {}
    tokens["employer"], _ = accounts.register("hr@acme.com", "password123",
                                              role="employer", org_name="Acme Corp")
    tokens["coordinator"], _ = accounts.create_user("c@york.ca", "password123",
                                                    role="coordinator")
    for i in range(6):
        tokens[f"s{i}"], _ = accounts.register(f"s{i}@york.ca", "password123")
    return TestClient(create_app()), tokens, accounts


def _as(client, tokens, who):
    client.cookies.set("rm_session", tokens[who])
    return client


def _wait_jobs():
    deadline = time.time() + 20
    while time.time() < deadline:
        with closing(connect()) as conn:
            if not conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN "
                                "('queued','running')").fetchone()[0]:
                return
        time.sleep(0.05)


def test_self_id_lands_only_in_the_audit_file(platform, tmp_path):
    client, tokens, _ = platform
    _as(client, tokens, "s0")
    # consent gate first
    assert client.post("/api/students/me/self-id",
                       json={"attrs": {"gender": "woman"}}).status_code == 409
    client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": True})
    assert client.post("/api/students/me/self-id",
                       json={"attrs": {"gender": "woman", "first_generation": "yes"}}).json() \
        == {"stored": 2}
    # unknown attribute rejected (only the enumerated auditable set exists)
    assert client.post("/api/students/me/self-id",
                       json={"attrs": {"favourite_color": "blue"}}).status_code == 400

    # physically separate: the audit file has the row; the PLATFORM db has no self_id table
    with closing(sqlite3.connect(tmp_path / "audit.db")) as audit:
        assert audit.execute("SELECT COUNT(*) FROM self_id").fetchone()[0] == 2
    with closing(connect()) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "self_id" not in tables

    # delete-my-self-ID
    assert client.request("DELETE", "/api/students/me/self-id").json()["deleted"] is True
    with closing(sqlite3.connect(tmp_path / "audit.db")) as audit:
        assert audit.execute("SELECT COUNT(*) FROM self_id").fetchone()[0] == 0


def test_min_cell_suppression():
    audit = AuditDB()
    refs = [f"student-{i}" for i in range(9)]
    for ref in refs[:5]:
        audit.set_self_id(ref, {"gender": "woman"})
    for ref in refs[5:9]:
        audit.set_self_id(ref, {"gender": "man"})
    agg = audit.aggregate(refs, "gender")
    assert agg["counts"] == {"woman": 5}          # 5 meets MIN_CELL
    assert agg["suppressed_cells"] == 1           # the 4-member cell is hidden
    assert agg["responses"] == 9                  # totals stay honest
    with pytest.raises(ValueError):
        audit.aggregate(refs, "postal_code")      # not an auditable attribute


def test_funnel_report_counts_and_csv(platform):
    client, tokens, accounts = platform
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={
        "fields": {"title": "Dev Intern", "description": "Python and SQL."},
        "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/postings/{pid}/approve")

    # two applicants; one gets shortlisted; employer views the shortlist (exposure)
    for who in ("s0", "s1"):
        _as(client, tokens, who)
        for purpose in ("resume_storage", "profile_matching"):
            client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
        client.post("/api/students/me/resume",
                    files={"resume": ("r.txt", RESUME.encode(), "text/plain")})
        client.post(f"/api/postings/{pid}/apply")
    _wait_jobs()
    _as(client, tokens, "employer")
    shortlist = client.get(f"/api/postings/{pid}/shortlist").json()["shortlist"]
    applied = [r for r in shortlist if r["application_id"]]
    client.patch(f"/api/applications/{applied[0]['application_id']}",
                 json={"status": "shortlisted"})

    _as(client, tokens, "coordinator")
    report = client.get("/api/coordinator/reports/funnel").json()["postings"]
    row = next(r for r in report if r["id"] == pid)
    assert row["applied"] == 2
    assert row["shortlisted_or_beyond"] == 1
    assert row["candidates_scored"] == 2
    assert row["shortlist_viewers"] == 1          # the exposure event
    assert row["selection_rate"] == 0.5

    csv_text = client.get("/api/coordinator/reports/funnel?format=csv").text
    assert csv_text.splitlines()[0].startswith("posting_id,title,employer")
    assert "Dev Intern" in csv_text

    _as(client, tokens, "employer")
    assert client.get("/api/coordinator/reports/funnel").status_code == 403


def test_self_id_report_uses_aligned_refs(platform):
    client, tokens, accounts = platform
    org_id = accounts.user_for_token(tokens["employer"])["org_id"]
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={
        "fields": {"title": "Dev", "description": "Python."},
        "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tokens, "coordinator")
    client.post(f"/api/coordinator/postings/{pid}/approve")

    # 5 applicants self-ID the same way -> visible cell; a 6th self-IDs but never applies
    for i in range(5):
        who = f"s{i}"
        _as(client, tokens, who)
        for purpose in ("resume_storage", "profile_matching", "self_id_audit"):
            client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
        client.post("/api/students/me/resume",
                    files={"resume": ("r.txt", RESUME.encode(), "text/plain")})
        client.post(f"/api/postings/{pid}/apply")
        client.post("/api/students/me/self-id", json={"attrs": {"first_generation": "yes"}})
    _as(client, tokens, "s5")
    client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": True})
    client.post("/api/students/me/self-id", json={"attrs": {"first_generation": "yes"}})
    _wait_jobs()

    _as(client, tokens, "coordinator")
    report = client.get("/api/coordinator/reports/self-id").json()
    assert report["applicants"] == 5
    fg = report["attributes"]["first_generation"]
    assert fg["counts"] == {"yes": 5}             # the non-applicant's row is NOT in the report
    assert fg["responses"] == 5
