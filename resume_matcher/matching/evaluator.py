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


def evaluate(
    candidate: CandidateProfile,
    job: JobSpec,
    adapter: InferenceAdapter | None = None,
) -> ScoreResult:
    adapter = adapter or get_adapter()

    # Anti-gaming checks run on the resume text BEFORE/independent of the LLM. These produce
    # advisory flags only — never an auto-reject (plan §D).
    flags: list[str] = []
    flags += scan_injection(candidate.text)
    flags += scan_keyword_stuffing(candidate.text, job)

    extraction = adapter.extract(candidate, job)
    return ranker.score(extraction, candidate, job, extra_flags=flags)


def evaluate_many(
    candidates: list[CandidateProfile],
    job: JobSpec,
    adapter: InferenceAdapter | None = None,
) -> list[ScoreResult]:
    adapter = adapter or get_adapter()
    return [evaluate(c, job, adapter) for c in candidates]
