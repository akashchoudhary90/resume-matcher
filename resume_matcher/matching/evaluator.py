"""Evaluator: orchestrates one (candidate, job) scoring.

It runs the anti-gaming checks, calls the LLM adapter for structured EXTRACTION, then hands the
extraction to the deterministic ranker. The LLM's output is advisory data; the ranker decides.
"""
from __future__ import annotations

from ..antigaming.injection import scan_injection
from ..antigaming.keyword_stuffing import scan_keyword_stuffing
from ..inference.adapter import InferenceAdapter, get_adapter
from ..inference.schema import CandidateProfile, JobSpec, ScoreResult
from . import ranker


def antigaming_flags(text: str, job: JobSpec) -> list[str]:
    """Advisory anti-gaming flags from the resume text (injection + keyword-stuffing). Pure code, so
    it cannot itself be injected; the ranker decides whether/how they affect the score."""
    return scan_injection(text) + scan_keyword_stuffing(text, job)


def score_with_antigaming(
    extraction,
    candidate: CandidateProfile,
    job: JobSpec,
    extra_flags: list[str] | None = None,
) -> ScoreResult:
    """The single scoring chokepoint: run the anti-gaming scans on the candidate text, merge any
    caller-supplied flags (e.g. the demo's hidden-text / file-direct signals), and hand everything to
    the deterministic ranker. Used by both evaluate() and the ephemeral demo so they cannot drift."""
    flags = list(extra_flags or []) + antigaming_flags(candidate.text, job)
    return ranker.score(extraction, candidate, job, extra_flags=flags)


def evaluate(
    candidate: CandidateProfile,
    job: JobSpec,
    adapter: InferenceAdapter | None = None,
) -> ScoreResult:
    adapter = adapter or get_adapter()
    return score_with_antigaming(adapter.extract(candidate, job), candidate, job)


def evaluate_many(
    candidates: list[CandidateProfile],
    job: JobSpec,
    adapter: InferenceAdapter | None = None,
) -> list[ScoreResult]:
    adapter = adapter or get_adapter()
    return [evaluate(c, job, adapter) for c in candidates]
