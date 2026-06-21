"""The score must be fully explainable and the breakdown must reconcile EXACTLY to the number.

These tests pin the "valid reasoning the user can see" contract: every point is attributed to a job
skill, evidence quotes are verbatim, unverifiable claims are not counted, and the displayed math
adds up to fit_score.
"""
from resume_matcher.inference.schema import (
    CandidateProfile,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    SkillEvidence,
)
from resume_matcher.matching import ranker
from resume_matcher.matching.evaluator import evaluate


def _job(req, pref=None, min_edu=None):
    return JobSpec(
        job_id="J1", title="Dev", employer="Acme",
        required_skills=req, preferred_skills=pref or [], min_education=min_edu,
    )


def test_explanation_reconciles_to_fit_score():
    cand = CandidateProfile(
        candidate_id="C1",
        education_level="bachelor",
        years_experience=3,
        text="Built services in Python and SQL. " * 10,  # >200 chars, quotable python + sql
    )
    result = evaluate(cand, _job(["python", "sql", "docker"], ["react"]))
    ex = result.explanation
    assert ex is not None
    # The headline math the user checks must hold exactly.
    assert ex.final_score == result.fit_score
    assert round(ex.subtotal * ex.education_factor, 1) == result.fit_score
    assert round(ex.required_earned + ex.preferred_earned, 2) == ex.subtotal


def test_every_job_skill_has_a_component_with_evidence_or_reason():
    cand = CandidateProfile(
        candidate_id="C1", education_level="bachelor", years_experience=2,
        text="Strong Python developer. Wrote SQL queries daily. " * 6,
    )
    job = _job(["python", "sql", "docker"], ["react"])
    result = evaluate(cand, job)
    comps = {c.skill_id: c for c in result.explanation.components if c.bucket in ("required", "preferred")}
    for sid in job.required_skills + job.preferred_skills:
        assert sid in comps, f"missing breakdown line for {sid}"

    py = comps["python"]
    assert py.verified and py.status == MatchStatus.match
    assert py.evidence_span and py.evidence_span.lower() in cand.text.lower()  # verbatim quote
    assert py.points_earned > 0

    docker = comps["docker"]  # not in resume
    assert docker.status == MatchStatus.missing and docker.points_earned == 0.0


def test_unverifiable_claim_is_not_counted():
    """A claimed match whose quote is NOT in the resume must contribute 0 and be flagged."""
    cand = CandidateProfile(candidate_id="C1", education_level="bachelor", text="x" * 250)
    extraction = MatchExtraction(
        candidate_id="C1", job_id="J1",
        skill_matches=[
            SkillEvidence(
                skill_id="python", skill_name="Python", status=MatchStatus.match,
                evidence_span="I am an expert in Python",  # NOT present in cand.text
            )
        ],
    )
    result = ranker.score(extraction, cand, _job(["python"]))
    comp = next(c for c in result.explanation.components if c.skill_id == "python")
    assert comp.verified is False
    assert comp.points_earned == 0.0
    assert any(f.startswith("unverifiable_evidence") for f in result.flags)
    assert result.fit_score == 0.0  # the only required skill was not counted


def test_education_penalty_is_shown_and_applied():
    cand = CandidateProfile(
        candidate_id="C1", education_level="diploma", years_experience=1,
        text="Python and SQL and Docker everywhere. " * 8,
    )
    result = evaluate(cand, _job(["python", "sql", "docker"], min_edu="bachelor"))
    ex = result.explanation
    assert ex.education_factor == 0.85
    assert "below_min_education" in result.flags
    assert round(ex.subtotal * 0.85, 1) == result.fit_score


def test_empty_required_skills_renormalizes_to_preferred():
    """With no required skills, preferred carries the full 100 pts (no free points)."""
    cand = CandidateProfile(candidate_id="C1", text="Strong Python developer. " * 20)
    result = evaluate(cand, _job([], ["python"]))
    assert "no_required_skills" in result.flags
    ex = result.explanation
    assert ex.preferred_possible == 100.0 and ex.required_possible == 0.0
    assert result.fit_score == 100.0  # python is present and verified -> full preferred credit


def test_no_skills_at_all_scores_zero():
    cand = CandidateProfile(candidate_id="C1", text="anything " * 40)
    result = evaluate(cand, _job([], []))
    assert result.fit_score == 0.0
    assert any(c.bucket == "info" for c in result.explanation.components)


def _scored(n, status=MatchStatus.match):
    text = " ".join(f"word{i}" for i in range(n)) + " " + "x" * 250
    matches = [
        SkillEvidence(skill_id=f"sk{i}", skill_name=f"sk{i}", status=status, evidence_span=f"word{i}")
        for i in range(n)
    ]
    ext = MatchExtraction(candidate_id="C", job_id="J", skill_matches=matches)
    cand = CandidateProfile(candidate_id="C", text=text)
    job = JobSpec(job_id="J", title="t", employer="e", required_skills=[f"sk{i}" for i in range(n)])
    return ranker.score(ext, cand, job)


def test_breakdown_rows_sum_to_bucket_totals():
    # The displayed per-row points must add up to the shown bucket totals for every skill count.
    for n in (1, 3, 6, 7, 9, 11, 12):
        for status in (MatchStatus.match, MatchStatus.partial):
            ex = _scored(n, status).explanation
            rows = [c for c in ex.components if c.bucket == "required"]
            assert round(sum(c.points_earned for c in rows), 2) == ex.required_earned
            assert round(sum(c.points_possible for c in rows), 2) == ex.required_possible


def test_education_row_reconciles_on_penalty():
    cand = CandidateProfile(
        candidate_id="C", education_level="diploma", text="python java sql " + "x" * 250
    )
    ext = MatchExtraction(
        candidate_id="C", job_id="J",
        skill_matches=[
            SkillEvidence(skill_id="python", skill_name="Python", status=MatchStatus.match, evidence_span="python"),
            SkillEvidence(skill_id="java", skill_name="Java", status=MatchStatus.match, evidence_span="java"),
            SkillEvidence(skill_id="sql", skill_name="SQL", status=MatchStatus.partial, evidence_span="sql"),
        ],
    )
    job = JobSpec(job_id="J", title="t", employer="e",
                  required_skills=["python", "java", "sql", "aws"], preferred_skills=["docker"],
                  min_education="bachelor")
    res = ranker.score(ext, cand, job)
    ex = res.explanation
    assert ex.education_factor == 0.85
    # displayed math reconciles: subtotal x all multipliers == fit_score
    assert round(ex.subtotal * ex.education_factor * ex.experience_factor * ex.must_have_factor, 1) == res.fit_score


def test_duplicate_skill_ids_collapsed():
    cand = CandidateProfile(candidate_id="C", text="python " * 60)
    res = evaluate(cand, _job(["python", "python"], ["python"]))
    assert "duplicate_skill_ids_collapsed" in res.flags
    assert len([c for c in res.explanation.components if c.bucket == "required"]) == 1
    assert [c for c in res.explanation.components if c.bucket == "preferred"] == []
    assert res.fit_score == 100.0


def test_duplicate_evidence_keeps_strongest_regardless_of_order():
    cand = CandidateProfile(candidate_id="C", text="Expert in Python. Python basics. " + "x" * 250)
    evs = [
        SkillEvidence(skill_id="python", skill_name="P", status=MatchStatus.match, evidence_span="Expert in Python"),
        SkillEvidence(skill_id="python", skill_name="P", status=MatchStatus.partial, evidence_span="Python basics"),
    ]
    fwd = ranker.score(MatchExtraction(candidate_id="C", job_id="J", skill_matches=evs), cand, _job(["python"]))
    rev = ranker.score(MatchExtraction(candidate_id="C", job_id="J", skill_matches=list(reversed(evs))), cand, _job(["python"]))
    assert fwd.fit_score == rev.fit_score == 100.0  # strongest match wins, order-independent
    assert any(f.startswith("duplicate_skill_evidence") for f in fwd.flags)


def _ev(sid, span, status=MatchStatus.match):
    return SkillEvidence(skill_id=sid, skill_name=sid, status=status, evidence_span=span)


def test_must_have_missing_heavily_penalized_and_flagged():
    job = JobSpec(job_id="J", title="t", employer="e",
                  required_skills=["python", "sql"], must_have_skills=["python"], preferred_skills=["docker"])
    cand = CandidateProfile(candidate_id="C", text="sql and docker expert " + "x" * 250, education_level="bachelor")
    ext = MatchExtraction(candidate_id="C", job_id="J", skill_matches=[_ev("sql", "sql"), _ev("docker", "docker")])
    res = ranker.score(ext, cand, job)
    assert res.explanation.must_have_factor == 0.4
    assert any(f == "missing_must_have:python" for f in res.flags)
    assert res.fit_score < 50  # deal-breaker missing -> not shortlist-worthy


def test_must_have_weighs_double_within_required():
    job = JobSpec(job_id="J", title="t", employer="e",
                  required_skills=["python", "sql"], must_have_skills=["python"])
    cand = CandidateProfile(candidate_id="C", text="python sql " + "x" * 250)
    ext = MatchExtraction(candidate_id="C", job_id="J", skill_matches=[_ev("python", "python"), _ev("sql", "sql")])
    comps = {c.skill_id: c.points_possible for c in ranker.score(ext, cand, job).explanation.components}
    assert abs(comps["python"] - 2 * comps["sql"]) < 0.1  # must-have weighs ~2x (allow rounding)


def test_experience_below_minimum_graded_penalty():
    job = JobSpec(job_id="J", title="t", employer="e", required_skills=["python"], min_years=4)
    cand = CandidateProfile(candidate_id="C", text="python developer " + "x" * 250, years_experience=1.0)
    ext = MatchExtraction(candidate_id="C", job_id="J", skill_matches=[_ev("python", "python")])
    res = ranker.score(ext, cand, job)
    assert 0.7 <= res.explanation.experience_factor < 1.0
    assert "below_min_experience" in res.flags
    assert round(res.explanation.subtotal * res.explanation.experience_factor, 1) == res.fit_score


def test_off_spec_claim_not_counted():
    cand = CandidateProfile(candidate_id="C", text="I know Python well. " + "x" * 250)
    ext = MatchExtraction(
        candidate_id="C", job_id="J",
        skill_matches=[
            SkillEvidence(skill_id="python", skill_name="P", status=MatchStatus.match, evidence_span="Python"),
            # off-spec skill not in the job, borrowing a real resume quote:
            SkillEvidence(skill_id="java", skill_name="J", status=MatchStatus.match, evidence_span="Python"),
        ],
    )
    res = ranker.score(ext, cand, _job(["python"]))
    assert res.subscores["verified_match_count"] == 1.0
    assert [m.skill_id for m in res.verified_matches] == ["python"]


def test_discarded_evidence_span_not_retained_but_clipped_when_verified():
    long_quote = "Python " + "z" * 400  # >160 chars, present in text
    cand = CandidateProfile(candidate_id="C", text=long_quote + " more text " * 10)
    ext = MatchExtraction(
        candidate_id="C", job_id="J",
        skill_matches=[
            SkillEvidence(skill_id="python", skill_name="P", status=MatchStatus.match, evidence_span=long_quote),
            SkillEvidence(skill_id="sql", skill_name="S", status=MatchStatus.match, evidence_span="NOT IN RESUME"),
        ],
    )
    res = ranker.score(ext, cand, _job(["python", "sql"]))
    py = next(c for c in res.explanation.components if c.skill_id == "python")
    sql = next(c for c in res.explanation.components if c.skill_id == "sql")
    assert py.evidence_span is not None and len(py.evidence_span) <= 161  # clipped (+ ellipsis)
    assert sql.evidence_span is None  # ungrounded discarded span not retained
