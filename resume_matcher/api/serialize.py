"""Shared serialization for a scored candidate.

One place that turns a `ScoreResult` (+ optional coaching) into the JSON the front-end renders, so
the synthetic-data dashboard and the ephemeral client demo show IDENTICAL, fully-explained results.
Crucially this includes the point-by-point `explanation` and the plain-English `flags_explained`,
which is what makes every score auditable by the user (request: "valid reasoning the user can see").
"""
from __future__ import annotations

import csv
import io

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


_CSV_HEADER = ["Rank", "Candidate", "Fit score", "Grade", "Confidence", "Education",
               "Years experience", "Skills found", "Integrity", "Missing must-have", "Top gap"]


def _integrity_label(flags_explained: list[dict] | None) -> str:
    """The same Clean / Review / Flagged signal the leaderboard badge shows (advisory, never a verdict)."""
    severities = {f.get("severity") for f in (flags_explained or [])}
    if "bad" in severities:
        return "Flagged"
    if "warn" in severities:
        return "Review"
    return "Clean"


def shortlist_csv(session: dict) -> str:
    """Render a scored session's ranked results as a shortlist CSV — built ENTIRELY in memory (the
    caller streams the string; nothing is written to disk). Summary columns only (no evidence quotes),
    and the header says "Fit score", never "match %": the number is an honest fit-readiness score, not
    a hire probability."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADER)
    for rank, row in enumerate(session.get("results") or [], start=1):
        gaps = row.get("blocking_gaps") or row.get("gaps") or []
        top_gap = gaps[0].get("skill", "") if gaps else ""
        years = row.get("years_experience")
        skills_found = row.get("skills_found")
        writer.writerow([
            rank,
            row.get("label", ""),
            row.get("fit_score", ""),
            row.get("grade", ""),
            row.get("confidence", ""),
            row.get("education_level") or "",
            "" if years is None else years,
            "" if skills_found is None else skills_found,
            _integrity_label(row.get("flags_explained")),
            "yes" if "missing_must_have" in (row.get("flags") or []) else "no",
            top_gap,
        ])
    return buf.getvalue()
