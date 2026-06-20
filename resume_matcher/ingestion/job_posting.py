"""Turn a pasted/free-text job posting into a JobSpec.

The coordinator pastes a posting; we auto-detect canonical skills (via the taxonomy) and infer a
minimum education level. By default every detected skill is treated as REQUIRED, but the demo UI
lets the user re-tag each skill required/preferred and add/remove skills before scoring — so the
inputs that drive the (fully explainable) score are the user's own, not a black box.

Skills are normalized to canonical IDs so synonyms collapse and keyword-stuffing in the posting or
the resume gains nothing (matching/taxonomy.py).
"""
from __future__ import annotations

from ..inference.schema import JobSpec
from ..matching.taxonomy import canonical_name, normalize_skills
from .parser import infer_education_level


def detect_job_skills(text: str) -> list[str]:
    """Canonical skill IDs mentioned anywhere in the posting (sorted, de-duplicated)."""
    return normalize_skills(text or "")


def build_job_spec(
    *,
    job_id: str = "JOB",
    title: str = "",
    employer: str = "",
    description: str = "",
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    min_education: str | None = None,
) -> JobSpec:
    """Assemble a JobSpec from (already-decided) fields, normalizing skill ids and dropping any skill
    that appears in both buckets from `preferred` so it is counted once, as required."""
    req = _dedupe([s for s in (required_skills or []) if s])
    pref = [s for s in _dedupe([s for s in (preferred_skills or []) if s]) if s not in set(req)]
    return JobSpec(
        job_id=job_id or "JOB",
        title=title.strip() or "Untitled role",
        employer=employer.strip() or "Unspecified employer",
        description=(description or "").strip(),
        required_skills=req,
        preferred_skills=pref,
        min_education=(min_education or None),
    )


def parse_job_posting(
    text: str,
    *,
    job_id: str = "JOB",
    title: str = "",
    employer: str = "",
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    min_education: str | None = None,
) -> tuple[JobSpec, list[str]]:
    """Parse a posting into (JobSpec, detected_skill_ids).

    If `required_skills`/`preferred_skills` are not supplied, all detected skills default to
    required. `min_education` is inferred from the text when not supplied."""
    detected = detect_job_skills(text)
    if required_skills is None and preferred_skills is None:
        required_skills = detected
    spec = build_job_spec(
        job_id=job_id,
        title=title,
        employer=employer,
        description=text or "",
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        min_education=min_education or infer_education_level(text or ""),
    )
    return spec, detected


def skill_options(skill_ids: list[str]) -> list[dict]:
    """[{id, name}, ...] for rendering selectable skill chips in the UI."""
    return [{"id": sid, "name": canonical_name(sid)} for sid in skill_ids]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
