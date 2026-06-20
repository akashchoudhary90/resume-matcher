"""Shared serialization for a scored candidate.

One place that turns a `ScoreResult` (+ optional coaching) into the JSON the front-end renders, so
the synthetic-data dashboard and the ephemeral client demo show IDENTICAL, fully-explained results.
Crucially this includes the point-by-point `explanation` and the plain-English `flags_explained`,
which is what makes every score auditable by the user (request: "valid reasoning the user can see").
"""
from __future__ import annotations

from ..inference.schema import ScoreResult
from ..matching.flag_text import humanize_flags


def result_to_dict(result: ScoreResult, coaching: dict | None = None, label: str | None = None) -> dict:
    """Serialize one scored candidate for the UI. `label` is an optional human display name
    (e.g. the uploaded filename) shown instead of the raw candidate_id."""
    out = {
        "candidate_id": result.candidate_id,
        "label": label or result.candidate_id,
        "fit_score": result.fit_score,
        "grade": result.grade,
        "confidence": result.confidence.value,
        "score_kind": result.score_kind,
        "subscores": result.subscores,
        "explanation": result.explanation.model_dump() if result.explanation else None,
        "verified_skills": [m.skill_name for m in result.verified_matches],
        "discarded_skills": [m.skill_name for m in result.discarded_matches],
        "gaps": [
            {
                "skill": g.skill_name,
                "importance": g.importance.value,
                "difficulty": g.acquisition_difficulty.value,
                "action": g.suggested_action,
            }
            for g in result.gaps
        ],
        "rationale": result.rationale,
        "flags": result.flags,
        "flags_explained": humanize_flags(result.flags),
    }
    if coaching is not None:
        out["blocking_gaps"] = coaching.get("blocking_gaps", [])
        out["next_actions"] = coaching.get("next_actions", [])
    return out
