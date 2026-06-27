"""Counterfactual gap-closing engine: the EXACT, fewest-change path to the next grade up."""
from resume_matcher.inference.schema import (
    CandidateProfile,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    SkillEvidence,
)
from resume_matcher.matching import ranker
from resume_matcher.matching.counterfactual import gap_to_next_grade


def _score(required=None, have=None, *, must_have=None, preferred=None,
           min_education=None, cand_education=None, min_years=None, cand_years=0.0):
    required = required or []
    have = have or []
    job = JobSpec(
        job_id="J", title="t", employer="e",
        required_skills=required, must_have_skills=must_have or [], preferred_skills=preferred or [],
        min_education=min_education, min_years=min_years,
    )
    text = "Resume. " + " ".join(f"I used {s} extensively." for s in have)
    cand = CandidateProfile(candidate_id="C", skills=list(have), text=text,
                            education_level=cand_education, years_experience=cand_years)
    matches = [SkillEvidence(skill_id=s, skill_name=s, status=MatchStatus.match,
                             evidence_span=f"I used {s} extensively.") for s in have]
    result = ranker.score(MatchExtraction(candidate_id="C", job_id="J", skill_matches=matches), cand, job)
    return result, cand, job


def test_one_skill_closes_to_next_grade():
    # 2 of 4 equal required skills -> 50.0 = grade C; acquiring one more -> 75 = grade B.
    result, cand, job = _score(["python", "sql", "docker", "kafka"], ["python", "sql"])
    assert result.grade == "C"
    gap = gap_to_next_grade(result, cand, job)
    assert gap["target_grade"] == "B"
    assert len(gap["steps"]) == 1
    assert gap["steps"][0]["kind"] == "skill"
    assert gap["projected_score"] >= 65          # exact re-score crosses the boundary
    # the projection is EXACT: re-scoring with that skill genuinely present yields the same number
    acquired = gap["steps"][0]["skill"]
    re_result, _, _ = _score(["python", "sql", "docker", "kafka"], ["python", "sql", acquired.lower()])
    assert abs(re_result.fit_score - gap["projected_score"]) < 0.05


def test_grade_a_returns_none():
    result, cand, job = _score(["python", "sql"], ["python", "sql"])   # 100 -> A
    assert result.grade == "A"
    assert gap_to_next_grade(result, cand, job) is None


def test_missing_must_have_is_the_lever():
    # All listed skills present but a must-have deal-breaker missing -> heavy penalty -> grade D.
    result, cand, job = _score(["python", "sql"], ["python", "sql"], must_have=["docker"])
    assert result.grade == "D"
    gap = gap_to_next_grade(result, cand, job)
    assert gap is not None
    assert any("docker" in s["label"].lower() for s in gap["steps"])


def test_experience_is_a_lever():
    # All skills present (subtotal 100) but below min years -> 0.7x -> 70 = B; reaching the years -> A.
    result, cand, job = _score(["python", "sql"], ["python", "sql"], min_years=4, cand_years=0)
    assert result.grade == "B"
    gap = gap_to_next_grade(result, cand, job)
    assert gap["target_grade"] == "A"
    assert any(s["kind"] == "experience" for s in gap["steps"])
    assert gap["projected_score"] >= 80


def test_not_reachable_within_max_changes_returns_none():
    # 0 of 8 skills -> grade D; reaching C needs 4 acquisitions, beyond the 3-change cap -> no false hope.
    result, cand, job = _score([f"skill{i}" for i in range(8)], [])
    assert result.grade == "D"
    assert gap_to_next_grade(result, cand, job) is None


def test_honest_phrasing_acquire_not_add_text():
    result, cand, job = _score(["python", "sql", "docker", "kafka"], ["python", "sql"])
    gap = gap_to_next_grade(result, cand, job)
    assert gap["steps"][0]["label"].startswith("acquire ")    # capability, not "add text to résumé"
