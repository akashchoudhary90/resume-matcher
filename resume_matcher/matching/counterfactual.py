"""Counterfactual gap-closing engine — the EXACT minimal change to reach the next grade.

Because the ranker is deterministic and reconciles every point, we can search it for the smallest set
of acquirable changes (acquire a missing job skill / meet the minimum education / reach the minimum
years) that flips a candidate across the next grade boundary, and re-score each hypothesis on the SAME
ranker code — so the projected grade is EXACT, not a SHAP-style approximation. A black-box "match %"
cannot produce a faithful counterfactual; this is a pure dividend of the glass-box ranker.

Honesty guardrail: this answers "what capability/EVIDENCE would you need to ACQUIRE", never "what text
to add to a résumé". The projection re-runs the same verbatim-evidence-verifying ranker, so it cannot be
gamed by editing a document — only by genuinely acquiring the skill.
"""
from __future__ import annotations

from itertools import combinations

from ..inference.schema import (
    CandidateProfile,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    ScoreResult,
    SkillEvidence,
)
from . import ranker as _ranker
from .taxonomy import canonical_name

_GRADE_FLOOR = {"A": 80.0, "B": 65.0, "C": 50.0}   # minimum fit to reach this grade
_NEXT_GRADE = {"D": "C", "C": "B", "B": "A"}
_INTEGRITY_PREFIXES = ("stuffing:", "injection:", "hidden_text:")
_DIFF_COST = {"low": 1, "medium": 2, "high": 3}
_MAX_CHANGES = 3          # only ever suggest a path of up to this many changes ("you're close")
_MAX_LEVERS = 12          # cap the search space (keeps it fast + the message actionable)


def _difficulty_of(result: ScoreResult, skill_id: str) -> str:
    for g in result.gaps:
        if g.skill_id == skill_id:
            return g.acquisition_difficulty.value
    return "medium"


def gap_to_next_grade(result: ScoreResult, candidate: CandidateProfile, job: JobSpec) -> dict | None:
    """Cheapest, fewest-change path to the NEXT grade up — or None if already grade A or no path within
    `_MAX_CHANGES` (we never give false hope). Every projected score is an EXACT ranker re-score."""
    target_grade = _NEXT_GRADE.get(result.grade)
    if target_grade is None:
        return None  # already grade A — nothing to close
    target = _GRADE_FLOOR[target_grade]

    integ_flags = [f for f in result.flags if any(f.startswith(p) for p in _INTEGRITY_PREFIXES)]
    have_ids = {ev.skill_id for ev in result.verified_matches}
    job_skill_ids = list(dict.fromkeys(
        list(job.required_skills) + list(job.must_have_skills) + list(job.preferred_skills)))
    must_set = set(job.must_have_skills)

    levers: list[dict] = []
    for sid in (s for s in job_skill_ids if s not in have_ids):
        is_must = sid in must_set
        diff = _difficulty_of(result, sid)
        levers.append({
            "kind": "skill", "key": sid, "skill": canonical_name(sid), "difficulty": diff,
            "label": f"acquire {canonical_name(sid)}" + (" (deal-breaker)" if is_must else ""),
            "cost": _DIFF_COST.get(diff, 2), "priority": 0 if is_must else 1,
        })
    # Education lever — only when it is ACTUALLY penalizing (below a known minimum), matching the ranker.
    need_e, have_e = _ranker._edu_rank(job.min_education), _ranker._edu_rank(candidate.education_level)
    if need_e is not None and have_e is not None and have_e < need_e:
        levers.append({"kind": "education", "key": job.min_education,
                       "label": f"reach {job.min_education} education", "cost": 3, "priority": 2})
    # Experience lever — only when below the job's minimum.
    if job.min_years and job.min_years > 0 and (candidate.years_experience or 0.0) < job.min_years:
        levers.append({"kind": "experience", "key": float(job.min_years),
                       "label": f"reach {job.min_years:g}+ years experience", "cost": 2, "priority": 2})

    if not levers:
        return None
    levers.sort(key=lambda lv: (lv["priority"], lv["cost"]))  # must-haves first, then cheapest
    levers = levers[:_MAX_LEVERS]

    best: tuple[int, int, tuple, float] | None = None
    for k in range(1, min(_MAX_CHANGES, len(levers)) + 1):
        for combo in combinations(levers, k):
            projected = _score_hypothesis(result, candidate, job, combo, integ_flags)
            if projected >= target:
                cost = sum(lv["cost"] for lv in combo)
                if best is None or (k, cost) < (best[0], best[1]):
                    best = (k, cost, combo, projected)
        if best is not None:
            break  # the smallest k that crosses wins (fewest changes), then cheapest within it

    if best is None:
        return None  # not reachable in <= _MAX_CHANGES — stay honest, surface nothing

    _, _, combo, projected = best
    steps = [
        {"kind": lv["kind"], "label": lv["label"],
         **({"skill": lv["skill"], "difficulty": lv["difficulty"]} if lv["kind"] == "skill" else {})}
        for lv in combo
    ]
    return {
        "current_grade": result.grade, "current_score": result.fit_score,
        "target_grade": target_grade, "target_score": target,
        "projected_score": projected,
        "steps": steps,
        "summary": (f"{len(combo)} change(s) from grade {target_grade}: "
                    + " + ".join(lv["label"] for lv in combo) + f" → ~{projected:g}."),
    }


def _score_hypothesis(result: ScoreResult, candidate: CandidateProfile, job: JobSpec,
                      combo: tuple, integ_flags: list[str]) -> float:
    """Re-score on the REAL ranker with the combo's changes applied (acquired skills get a verifiable
    evidence span appended to the text; education/experience bumped). Exact by construction."""
    text = candidate.text
    matches = list(result.verified_matches)
    edu, years = candidate.education_level, candidate.years_experience
    for lv in combo:
        if lv["kind"] == "skill":
            marker = f"\n[counterfactual] {lv['skill']}"
            text += marker
            matches.append(SkillEvidence(skill_id=lv["key"], skill_name=lv["skill"],
                                         status=MatchStatus.match, evidence_span=marker.strip()))
        elif lv["kind"] == "education":
            edu = lv["key"]
        elif lv["kind"] == "experience":
            years = float(lv["key"])
    cand2 = candidate.model_copy(update={"text": text, "education_level": edu, "years_experience": years})
    extraction = MatchExtraction(candidate_id=candidate.candidate_id, job_id=job.job_id, skill_matches=matches)
    return _ranker.score(extraction, cand2, job, extra_flags=integ_flags).fit_score
