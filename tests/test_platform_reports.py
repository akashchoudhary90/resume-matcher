"""Slice W: the persistent audit plane (separate file), voluntary self-ID, min-cell suppression,
and the coordinator funnel/EEO reports.

Phase-5 slice S3 re-pinned the aggregate() expectations (cohort floor + banded totals); slice S5
adds the ROUTE-level half: the A5/A6 consent-revoke cascades, the A7 `network_analytics` cohort
filter (on intro-equity and network-coverage, deliberately NOT on self-id — FH-H3), the A8 snapshot
serving policy, and B7's withdrawn exclusion from the funnel.
"""
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
    # Phase 5 (A8): 9 responses is below the 2*MIN_CELL cohort floor — nothing is published, not
    # even the exact total (which, with one cell visible, would have named the other four people).
    agg = audit.aggregate(refs, "gender")
    assert agg["counts"] == {}
    assert agg["suppressed_cells"] == 2
    assert agg["responses"] is None and agg["note"] == "cohort below reporting floor"
    with pytest.raises(ValueError):
        audit.aggregate(refs, "postal_code")      # not an auditable attribute


def test_responses_banded_once_any_cell_is_suppressed():
    """A8 complementary suppression: an exact total plus the visible cells is arithmetic for the
    hidden one. Above the cohort floor the total degrades to a MIN_CELL band."""
    audit = AuditDB()
    refs = [f"student-{i}" for i in range(37)]
    for ref in refs[:20]:
        audit.set_self_id(ref, {"gender": "woman"})
    for ref in refs[20:34]:
        audit.set_self_id(ref, {"gender": "man"})
    for ref in refs[34:37]:
        audit.set_self_id(ref, {"gender": "nonbinary"})   # 3 -> suppressed cell
    agg = audit.aggregate(refs, "gender")
    assert agg["counts"] == {"woman": 20, "man": 14}
    assert agg["suppressed_cells"] == 1
    assert agg["responses"] == "35-40"             # NOT 37: 37 - 20 - 14 would name the 3
    # with nothing suppressed the exact total is safe again
    for ref in refs[34:37]:
        audit.set_self_id(ref, {"gender": "man"})
    clean = audit.aggregate(refs, "gender")
    assert clean["counts"] == {"woman": 20, "man": 17}
    assert clean["suppressed_cells"] == 0 and clean["responses"] == 37


def test_snapshot_pins_a_payload_and_reports_its_cohort_size():
    """A8 snapshots: the pinning primitive behind the route's serving policy — recomputing on every
    read lets a coordinator difference two reports across one student joining the cohort."""
    audit = AuditDB()
    assert audit.get_snapshot("self_id", 1) is None
    audit.save_snapshot("self_id", 1, {"counts": {"yes": 12}}, 30)
    snap = audit.get_snapshot("self_id", 1)
    assert snap["payload"] == {"counts": {"yes": 12}} and snap["refs_count"] == 30
    assert snap["computed_at"] > 0
    # same key re-saves in place; a different school keeps its own pin
    audit.save_snapshot("self_id", 1, {"counts": {"yes": 13}}, 31)
    audit.save_snapshot("self_id", 2, {"counts": {"yes": 99}}, 40)
    assert audit.get_snapshot("self_id", 1)["refs_count"] == 31
    assert audit.get_snapshot("self_id", 2)["payload"] == {"counts": {"yes": 99}}


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
    assert report["applicants"] == 5              # the non-applicant is NOT in the report's ref set
    fg = report["attributes"]["first_generation"]
    # Phase 5 (A8): the aligned ref set is 5 self-IDs — below the 2*MIN_CELL cohort floor, so the
    # report publishes the cell count only as "1 suppressed", with no total to difference against.
    assert fg["counts"] == {}
    assert fg["suppressed_cells"] == 1
    assert fg["responses"] is None and fg["note"] == "cohort below reporting floor"


# ==================================================================================================
# Phase 5 / A5 + A6 — a revoke DELETES what the consent authorized (docs/PHASE5.md §3.1)
# ==================================================================================================
def test_revoking_resume_storage_deletes_the_blob_and_the_scores(platform):
    """A5. Revoking storage while the blob and its derived match rows sit in the DB is a broken
    promise, not a consent — the toggle has to be the deletion."""
    client, tokens, _ = platform
    _as(client, tokens, "s0")
    for purpose in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", RESUME.encode(), "text/plain")})
    uid = get_account_store().user_for_token(tokens["s0"])["id"]
    with closing(connect()) as conn:      # seed a score so the cascade has something to remove
        conn.execute("INSERT INTO match_results(posting_id, student_id, fit_score, "
                     "result_json, computed_at) VALUES('p-x',?,0.5,'{}',?)", (uid, time.time()))
        conn.commit()
    assert client.get("/api/students/me/profile").json()["resume"] is not None

    client.post("/api/students/me/consents", json={"purpose": "resume_storage", "granted": False})
    assert client.get("/api/students/me/profile").json()["resume"] is None
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resumes WHERE user_id=?", (uid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM match_results WHERE student_id=?",
                            (uid,)).fetchone()[0] == 0


def test_revoking_self_id_audit_deletes_the_audit_row(platform, tmp_path):
    """A6. The self-ID lives in the OTHER plane, which is exactly why it was being missed: the
    platform-side revoke has to reach across and delete it."""
    client, tokens, _ = platform
    _as(client, tokens, "s0")
    client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": True})
    client.post("/api/students/me/self-id", json={"attrs": {"gender": "woman"}})
    uid = get_account_store().user_for_token(tokens["s0"])["id"]
    assert AuditDB().has_self_id(f"student-{uid}") is True

    client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": False})
    assert AuditDB().has_self_id(f"student-{uid}") is False
    with closing(sqlite3.connect(tmp_path / "audit.db")) as audit:
        assert audit.execute("SELECT COUNT(*) FROM self_id").fetchone()[0] == 0
    # re-granting does NOT resurrect it — the answer was deleted, not hidden
    client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": True})
    assert AuditDB().has_self_id(f"student-{uid}") is False


def test_revoking_profile_matching_still_drops_scores(platform):
    client, tokens, _ = platform
    _as(client, tokens, "s0")
    uid = get_account_store().user_for_token(tokens["s0"])["id"]
    client.post("/api/students/me/consents", json={"purpose": "profile_matching", "granted": True})
    with closing(connect()) as conn:
        conn.execute("INSERT INTO match_results(posting_id, student_id, fit_score, "
                     "result_json, computed_at) VALUES('p-y',?,0.5,'{}',?)", (uid, time.time()))
        conn.commit()
    client.post("/api/students/me/consents", json={"purpose": "profile_matching", "granted": False})
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM match_results WHERE student_id=?",
                            (uid,)).fetchone()[0] == 0


def test_storage_and_audit_purposes_never_route_through_the_graph_consent_endpoint():
    """The A5/A6 cascades live on ONE route. This pins the assumption that makes that safe: the
    graph consent endpoint's whitelist cannot accept a purpose whose revoke needs a cascade."""
    from resume_matcher.api.platform import _GRAPH_PURPOSES

    assert set(_GRAPH_PURPOSES).isdisjoint({"resume_storage", "profile_matching", "self_id_audit"})


def test_graph_consent_route_rejects_a_storage_purpose(platform):
    client, tokens, _ = platform
    _as(client, tokens, "s0")
    r = client.post("/api/graph/consents", json={"purpose": "resume_storage", "granted": False})
    assert r.status_code == 400


# ==================================================================================================
# Phase 5 / A7 + FH-H3 — the network_analytics cohort filter, applied to the RIGHT reports
# ==================================================================================================
def _live_posting(client, tokens, accounts):
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
    return pid


def _apply(client, tokens, who, pid, *, analytics: bool):
    _as(client, tokens, who)
    for purpose in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": purpose, "granted": True})
    if analytics:
        client.post("/api/graph/consents", json={"purpose": "network_analytics", "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", RESUME.encode(), "text/plain")})
    client.post(f"/api/postings/{pid}/apply")


def test_intro_equity_counts_only_the_analytics_consenting_cohort(platform):
    """A7. `network_analytics` is the consent that says 'you may count me in fairness aggregates'.
    Before this, the report counted everyone who had never been asked."""
    client, tokens, accounts = platform
    pid = _live_posting(client, tokens, accounts)
    for i, analytics in enumerate([True, True, False, False, False]):
        _apply(client, tokens, f"s{i}", pid, analytics=analytics)
    _wait_jobs()
    _as(client, tokens, "coordinator")
    report = client.get("/api/coordinator/reports/intro-equity").json()
    assert report["applicants"] == 2          # 5 applied; 3 never consented to being analyzed
    assert report["requested"] == 0 and report["converted"] == 0


def test_network_coverage_counts_only_the_analytics_consenting_cohort(platform):
    """FH-H3: the half A7 originally missed. Being discoverable is consent to be FOUND; counting
    someone's network position is analytics ABOUT them and needs the analytics consent too."""
    client, tokens, accounts = platform
    for i, analytics in enumerate([True, True, False]):
        _as(client, tokens, f"s{i}")
        client.post("/api/graph/consents", json={"purpose": "graph_discoverable", "granted": True})
        if analytics:
            client.post("/api/graph/consents",
                        json={"purpose": "network_analytics", "granted": True})
    _wait_jobs()
    _as(client, tokens, "coordinator")
    body = client.get("/api/coordinator/reports/network-coverage").json()
    assert body["discoverable_students"] == 2      # the third is discoverable but not analyzable
    assert body["network_poverty"] == 2            # ...and both consenting ones have zero edges
    assert "never self-ID-based" in body["note"]


def test_self_id_report_is_NOT_filtered_by_network_analytics(platform):
    """FH-H3 (the other direction). The self-ID report's basis is `self_id_audit` — the consent the
    respondents gave for exactly this aggregate. Layering network_analytics on top would empty it
    for every student who consented to the bias audit and nothing else."""
    client, tokens, accounts = platform
    pid = _live_posting(client, tokens, accounts)
    for i in range(3):
        _apply(client, tokens, f"s{i}", pid, analytics=False)   # NO network_analytics anywhere
        client.post("/api/students/me/consents", json={"purpose": "self_id_audit", "granted": True})
        client.post("/api/students/me/self-id", json={"attrs": {"first_generation": "yes"}})
    _wait_jobs()
    _as(client, tokens, "coordinator")
    report = client.get("/api/coordinator/reports/self-id").json()
    assert report["applicants"] == 3        # would be 0 if A7 were (wrongly) applied here
    assert report["attributes"]["first_generation"]["suppressed_cells"] == 1


# ==================================================================================================
# Phase 5 / A8 — snapshot serving (docs/PHASE5.md §2.6, §3.1)
# ==================================================================================================
def test_reports_are_served_from_a_pinned_snapshot(platform, monkeypatch):
    """A8. Suppression stops one read from naming anyone; it does nothing about DIFFERENCING two
    reads across a joiner. The pin is what closes that."""
    client, tokens, accounts = platform
    pid = _live_posting(client, tokens, accounts)
    _apply(client, tokens, "s0", pid, analytics=True)
    _wait_jobs()
    _as(client, tokens, "coordinator")
    first = client.get("/api/coordinator/reports/intro-equity").json()
    assert first["applicants"] == 1
    snap = AuditDB().get_snapshot("intro_equity", 1)
    assert snap is not None and snap["refs_count"] == 1

    # a second student joins the cohort: the served payload does NOT move (the pin is fresh)
    _apply(client, tokens, "s1", pid, analytics=True)
    _wait_jobs()
    _as(client, tokens, "coordinator")
    assert client.get("/api/coordinator/reports/intro-equity").json() == first

    # age alone doesn't unpin it: the cohort has moved by 1, and a 1-person delta IS the leak
    monkeypatch.setenv("RM_AUDIT_SNAPSHOT_HOURS", "0")
    assert client.get("/api/coordinator/reports/intro-equity").json()["applicants"] == 1

    # aged AND moved by >= MIN_CELL (1 -> 6): now a recompute can't be differenced to one person
    for i in range(2, 6):
        _apply(client, tokens, f"s{i}", pid, analytics=True)
    _wait_jobs()
    _as(client, tokens, "coordinator")
    assert client.get("/api/coordinator/reports/intro-equity").json()["applicants"] == 6
    assert AuditDB().get_snapshot("intro_equity", 1)["refs_count"] == 6


def test_self_id_report_pins_under_its_own_key(platform, monkeypatch):
    client, tokens, accounts = platform
    pid = _live_posting(client, tokens, accounts)
    _apply(client, tokens, "s0", pid, analytics=False)
    _wait_jobs()
    _as(client, tokens, "coordinator")
    client.get("/api/coordinator/reports/self-id")
    assert AuditDB().get_snapshot("self_id", 1)["refs_count"] == 1
    assert AuditDB().get_snapshot("intro_equity", 1) is None   # separate keys, separate pins


# ==================================================================================================
# Phase 5 / B7 — a withdrawal is not an employer selection decision
# ==================================================================================================
def test_funnel_excludes_withdrawn_from_applied(platform):
    client, tokens, accounts = platform
    pid = _live_posting(client, tokens, accounts)
    for i in range(3):
        _apply(client, tokens, f"s{i}", pid, analytics=False)
    _wait_jobs()
    _as(client, tokens, "employer")
    apps = client.get(f"/api/postings/{pid}/applications").json()["applications"]
    client.patch(f"/api/applications/{apps[0]['id']}", json={"status": "shortlisted"})

    _as(client, tokens, "s2")
    my_app = client.get("/api/students/me/applications").json()["applications"][0]
    client.post(f"/api/applications/{my_app['id']}/withdraw")

    _as(client, tokens, "coordinator")
    row = next(r for r in client.get("/api/coordinator/reports/funnel").json()["postings"]
               if r["id"] == pid)
    assert row["applied"] == 2 and row["withdrawn"] == 1
    assert row["selection_rate"] == 0.5      # 1/2, not 1/3 — the student's exit isn't a rejection

    csv_text = client.get("/api/coordinator/reports/funnel?format=csv").text
    assert csv_text.splitlines()[0].split(",")[5:7] == ["applied", "withdrawn"]
    assert csv_text.splitlines()[1].split(",")[5:7] == ["2", "1"]
