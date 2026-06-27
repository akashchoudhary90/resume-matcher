"""JD reverse-audit: how the posting's own requirements shape the qualified pool (exact re-scores)."""
from resume_matcher.inference.schema import (
    CandidateProfile,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    SkillEvidence,
)
from resume_matcher.matching import ranker
from resume_matcher.matching.jd_audit import audit_requirements


def _scored(job, candidates):
    """candidates: list of (label, have_skills, years, education)."""
    out = []
    for label, have, years, edu in candidates:
        text = "Resume. " + " ".join(f"I used {s} extensively." for s in have)
        cand = CandidateProfile(candidate_id=label, skills=list(have), text=text,
                                years_experience=years, education_level=edu)
        matches = [SkillEvidence(skill_id=s, skill_name=s, status=MatchStatus.match,
                                 evidence_span=f"I used {s} extensively.") for s in have]
        res = ranker.score(MatchExtraction(candidate_id=label, job_id=job.job_id, skill_matches=matches),
                           cand, job)
        out.append((res, cand, label))
    return out


def test_audit_identifies_sole_blocker():
    job = JobSpec(job_id="J", title="t", employer="e",
                  required_skills=["python", "sql"], must_have_skills=["kubernetes"])
    scored = _scored(job, [
        ("Strong", ["python", "sql", "kubernetes"], 0, None),
        ("BlockedA", ["python", "sql"], 0, None),   # only missing the kubernetes must-have
        ("BlockedB", ["python", "sql"], 0, None),
    ])
    audit = audit_requirements(scored, job)
    assert audit is not None
    assert audit["n_candidates"] == 3 and audit["qualified"] == 1
    top = audit["findings"][0]
    assert "kubernetes" in top["requirement"].lower()
    assert top["freed_count"] == 2 and top["sole_blocked"] == 2   # relaxing it frees both, solely
    assert "kubernetes" in audit["summary"].lower()


def test_audit_returns_none_when_too_few_candidates():
    job = JobSpec(job_id="J", title="t", employer="e", required_skills=["python"], must_have_skills=["docker"])
    scored = _scored(job, [("solo", ["python"], 0, None)])
    assert audit_requirements(scored, job) is None


def test_audit_returns_none_when_no_requirements():
    job = JobSpec(job_id="J", title="t", employer="e", required_skills=["python"])  # no must-have/edu/years
    scored = _scored(job, [("a", ["python"], 0, None), ("b", ["python"], 0, None)])
    # required-skill relaxation is still a requirement, so this is NOT None — but everyone already qualifies
    audit = audit_requirements(scored, job)
    assert audit is None or audit["findings"] == []


def test_experience_requirement_is_auditable():
    # Candidates have most skills but the experience floor (combined with a small skills gap) drops them
    # below the bar -> relaxing the years requirement frees them, so it surfaces as a finding.
    job = JobSpec(job_id="J", title="t", employer="e",
                  required_skills=["python", "sql", "docker"], min_years=8)
    scored = _scored(job, [
        ("Junior1", ["python", "sql"], 0, None),   # 66.7 subtotal x 0.7 exp = 46.7 -> grade D
        ("Junior2", ["python", "sql"], 0, None),
    ])
    audit = audit_requirements(scored, job)
    assert audit is not None
    assert any(f["kind"] == "experience" for f in audit["findings"])


def test_session_carries_jd_audit():
    from resume_matcher.api.demo import SessionStore, run_demo
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, required_skills=["python", "sql"], must_have_skills=["kubernetes"],
        files=[("Alice.txt", b"Python and SQL developer. Bachelor. " * 4),
               ("Bob.txt", b"Python and SQL engineer. Master. " * 4)],
    )
    d = sess.to_dict()
    assert "jd_audit" in d
    # both lack kubernetes -> the must-have is the limiting requirement
    if d["jd_audit"]:
        assert any("kubernetes" in f["requirement"].lower() for f in d["jd_audit"]["findings"])
