"""Deterministic ranker — where the scoring DECISION is made (never by the LLM).

It takes the LLM's MatchExtraction and:
  1. VERIFIES every claimed evidence span is a real verbatim substring of the candidate's text.
     Unverifiable matches are discarded and flagged — this defeats fabricated/injected skill claims.
  2. Computes an explainable fit/readiness score from coverage of essential vs preferred skills,
     with an education-level gate. The number is reproducible and decomposed into subscores.

This is the honesty boundary: `fit_score` is fit/readiness, NOT a predicted probability of hire.
"""
from __future__ import annotations

from ..inference.schema import (
    CandidateProfile,
    Confidence,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    ScoreResult,
    SkillEvidence,
)

_EDU_RANK = {
    "highschool": 0,
    "high school": 0,
    "diploma": 1,
    "certificate": 1,
    "associate": 2,
    "bachelor": 3,
    "bachelors": 3,
    "master": 4,
    "masters": 4,
    "phd": 5,
    "doctorate": 5,
}

_STATUS_WEIGHT = {MatchStatus.match: 1.0, MatchStatus.partial: 0.5, MatchStatus.missing: 0.0}


def _edu_rank(level: str | None) -> int | None:
    if not level:
        return None
    return _EDU_RANK.get(level.strip().lower())


def _verify(text: str, ev: SkillEvidence) -> bool:
    """A match/partial is verified only if its evidence span is genuinely present in the text."""
    if ev.status == MatchStatus.missing:
        return False
    if not ev.evidence_span:
        return False
    return ev.evidence_span.lower() in text.lower()


def score(
    extraction: MatchExtraction,
    candidate: CandidateProfile,
    job: JobSpec,
    extra_flags: list[str] | None = None,
) -> ScoreResult:
    flags = list(extra_flags or [])
    verified: list[SkillEvidence] = []
    discarded: list[SkillEvidence] = []

    for ev in extraction.skill_matches:
        if ev.status == MatchStatus.missing:
            continue
        if _verify(candidate.text, ev):
            verified.append(ev)
        else:
            discarded.append(ev)
            flags.append(f"unverifiable_evidence:{ev.skill_id}")

    verified_by_skill = {ev.skill_id: ev for ev in verified}

    def coverage(skill_ids: list[str]) -> float:
        if not skill_ids:
            return 1.0
        total = 0.0
        for sid in skill_ids:
            ev = verified_by_skill.get(sid)
            total += _STATUS_WEIGHT[ev.status] if ev else 0.0
        return total / len(skill_ids)

    required_cov = coverage(job.required_skills)
    preferred_cov = coverage(job.preferred_skills)

    # Education gate: below the stated minimum applies a penalty, never an outright proxy feature.
    edu_factor = 1.0
    need = _edu_rank(job.min_education)
    have = _edu_rank(candidate.education_level)
    if need is not None and have is not None and have < need:
        edu_factor = 0.85
        flags.append("below_min_education")

    base = 100.0 * (0.75 * required_cov + 0.25 * preferred_cov)
    fit = round(base * edu_factor, 1)

    grade = "A" if fit >= 80 else "B" if fit >= 65 else "C" if fit >= 50 else "D"

    # Confidence reflects how much we can trust the inputs, not how good the candidate is.
    if not candidate.has_resume or len(candidate.text) < 200:
        conf = Confidence.low
    elif discarded:
        conf = Confidence.medium
    else:
        conf = Confidence.high

    subscores = {
        "required_coverage": round(required_cov, 3),
        "preferred_coverage": round(preferred_cov, 3),
        "education_factor": edu_factor,
        "verified_match_count": float(len(verified)),
        "discarded_match_count": float(len(discarded)),
    }

    return ScoreResult(
        candidate_id=candidate.candidate_id,
        job_id=job.job_id,
        fit_score=fit,
        grade=grade,
        confidence=conf,
        subscores=subscores,
        verified_matches=verified,
        discarded_matches=discarded,
        gaps=extraction.gaps,
        rationale=extraction.rationale,
        flags=sorted(set(flags)),
    )
