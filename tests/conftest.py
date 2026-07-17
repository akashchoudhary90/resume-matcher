"""Shared test fixtures/helpers. `pythonpath = ["."]` in pyproject makes `resume_matcher` importable."""
from __future__ import annotations

import time

import pytest

from resume_matcher.inference.schema import CandidateProfile, JobSpec
from resume_matcher.matching.taxonomy import normalize_skills


@pytest.fixture(autouse=True)
def _isolate_accounts_db(tmp_path, monkeypatch):
    """Point BOTH SQLite planes at per-test temp files so the suite never writes to the repo and each
    test gets a fresh, isolated store.

    RM_ACCOUNTS_DB covers the scoring plane: `platform_db_path()` falls back to it. The audit plane
    needs its own var — `audit_db_path()` reads only RM_AUDIT_DB and otherwise lands on the repo's
    `data/audit.db`. That leak is not merely untidy: Phase-5 A8 pins reports as snapshots in the
    audit plane, so an un-isolated run can serve a snapshot a PREVIOUS test wrote and pass on stale
    data. Both planes stay physically separate (boundary #2) — separate files, just under tmp_path.
    """
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "accounts.db"))
    monkeypatch.setenv("RM_AUDIT_DB", str(tmp_path / "audit.db"))


def finish_demo_run(client, response, timeout: float = 30.0):
    """Follow the async demo contract: the scoring endpoints answer 202 and run in the background.
    Polls the session until it leaves 'running' and returns that FINAL GET response (status 200,
    body['status'] in {'done','error'}). Non-202 responses (400/402/403/413/429) are returned
    unchanged so error-path assertions keep working."""
    if response.status_code != 202:
        return response
    sid = response.json()["session_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        g = client.get(f"/api/demo/session/{sid}")
        if g.status_code != 200 or g.json().get("status") != "running":
            return g
        time.sleep(0.02)
    raise AssertionError("async demo run did not finish in time")


@pytest.fixture
def demo_finish():
    """The finish_demo_run helper as a fixture (conftest isn't importable from test modules)."""
    return finish_demo_run


def make_candidate(cid: str, text: str, **kw) -> CandidateProfile:
    return CandidateProfile(
        candidate_id=cid,
        skills=normalize_skills(text),
        text=text,
        education_level=kw.get("education_level", "bachelor"),
        years_experience=kw.get("years_experience", 2.0),
        has_resume=kw.get("has_resume", True),
    )


@pytest.fixture
def python_job() -> JobSpec:
    return JobSpec(
        job_id="J001",
        title="Software Engineering Intern",
        employer="MapleSoft",
        description="Work with Python, SQL and Git building REST APIs.",
        required_skills=["python", "sql", "git"],
        preferred_skills=["docker", "rest_api"],
        min_education="bachelor",
    )


@pytest.fixture
def strong_candidate() -> CandidateProfile:
    return make_candidate(
        "S1",
        "Experienced developer. Skills: Python, SQL, Git, Docker. Built REST APIs with Python and SQL.",
    )


@pytest.fixture
def weak_candidate() -> CandidateProfile:
    return make_candidate("S2", "Skills: Excel and Communication. Studied business.", years_experience=0)
