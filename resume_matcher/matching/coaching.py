"""Coaching layer — the highest-value, lowest-regulatory-risk surface (plan §B).

Turns a ScoreResult into actionable, candidate-facing guidance: which 1-2 gaps are blocking each
role, ranked by importance x acquisition difficulty, plus the roles a student is closest to
qualifying for. This is the "how do we get this student hired" output.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..inference.schema import Difficulty, Gap, Importance, JobSpec, ScoreResult

_IMPORTANCE_W = {Importance.essential: 3, Importance.important: 2, Importance.optional: 1}
_DIFFICULTY_W = {Difficulty.low: 1, Difficulty.medium: 2, Difficulty.high: 3}


def _gap_priority(g: Gap) -> int:
    # Higher importance, lower difficulty -> address first (high leverage).
    return _IMPORTANCE_W[g.importance] * (4 - _DIFFICULTY_W[g.acquisition_difficulty])


def prioritized_gaps(result: ScoreResult) -> list[Gap]:
    return sorted(result.gaps, key=_gap_priority, reverse=True)


def coach(result: ScoreResult, job: JobSpec) -> dict:
    gaps = prioritized_gaps(result)
    blocking = [g for g in gaps if g.importance == Importance.essential][:2]
    return {
        "job_id": job.job_id,
        "title": job.title,
        "fit_score": result.fit_score,
        "grade": result.grade,
        "score_kind": result.score_kind,
        "blocking_gaps": [
            {"skill": g.skill_name, "action": g.suggested_action, "difficulty": g.acquisition_difficulty.value}
            for g in blocking
        ],
        "next_actions": [g.suggested_action for g in gaps[:3] if g.suggested_action],
        "review_flags": result.flags,
    }


@dataclass
class ClosestFit:
    candidate_id: str
    ranked: list[dict] = field(default_factory=list)


def closest_fit(candidate_id: str, scored: list[tuple[JobSpec, ScoreResult]], top_n: int = 3) -> ClosestFit:
    """Given a candidate's scores across jobs, return the roles they are closest to qualifying for,
    each annotated with the 1-2 gaps blocking it."""
    ranked = sorted(scored, key=lambda jr: jr[1].fit_score, reverse=True)[:top_n]
    out = []
    for job, res in ranked:
        blocking = [g for g in prioritized_gaps(res) if g.importance == Importance.essential][:2]
        out.append(
            {
                "job_id": job.job_id,
                "title": job.title,
                "employer": job.employer,
                "fit_score": res.fit_score,
                "grade": res.grade,
                "blocking_gaps": [g.skill_name for g in blocking],
            }
        )
    return ClosestFit(candidate_id=candidate_id, ranked=out)
