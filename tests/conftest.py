"""Shared test fixtures/helpers. `pythonpath = ["."]` in pyproject makes `resume_matcher` importable."""
from __future__ import annotations

import pytest

from resume_matcher.inference.schema import CandidateProfile, JobSpec
from resume_matcher.matching.taxonomy import normalize_skills


@pytest.fixture(autouse=True)
def _isolate_accounts_db(tmp_path, monkeypatch):
    """Point the accounts SQLite DB at a per-test temp file so the suite never writes to the repo and
    each test gets a fresh, isolated accounts store."""
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "accounts.db"))


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
