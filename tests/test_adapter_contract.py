"""Contract test: every adapter must return schema-valid MatchExtraction for the same fixtures.

This is the proof of swappability (plan §C). The mock adapter always runs; ollama/openai_compat run
only when their backend env vars are set, so CI passes without a live model.
"""
import os

import jsonschema
import pytest

from resume_matcher.inference.adapter import get_adapter, parse_extraction
from resume_matcher.inference.schema import MatchExtraction, match_extraction_schema


def _backends():
    backends = ["mock"]
    if os.environ.get("RM_TEST_OLLAMA"):
        backends.append("ollama")
    if os.environ.get("RM_TEST_OPENAI"):
        backends.append("openai_compat")
    return backends


@pytest.mark.parametrize("backend", _backends())
def test_adapter_returns_schema_valid_extraction(backend, strong_candidate, python_job):
    adapter = get_adapter(backend)
    extraction = adapter.extract(strong_candidate, python_job)

    # Validates against the pinned JSON Schema — the actual adapter contract.
    jsonschema.validate(extraction.model_dump(mode="json"), match_extraction_schema())
    # IDs are pinned by the adapter wrapper, never trusted from the backend.
    assert extraction.candidate_id == strong_candidate.candidate_id
    assert extraction.job_id == python_job.job_id


def test_mock_finds_quotable_evidence(strong_candidate, python_job):
    extraction = get_adapter("mock").extract(strong_candidate, python_job)
    matched = [m for m in extraction.skill_matches if m.status.value == "match"]
    assert matched, "expected at least one evidenced skill match"
    for m in matched:
        assert m.evidence_span and m.evidence_span.lower() in strong_candidate.text.lower()


def test_parse_extraction_recovers_json_from_noisy_output(strong_candidate, python_job):
    # JSON wrapped in prose + code fences, with ids omitted — exercises both recovery and defaulting.
    raw = (
        "Here is the result:\n```json\n"
        '{"skill_matches":[],"gaps":[],"seniority_assessment":"","rationale":"ok"}\n```\n'
    )
    ext = parse_extraction(raw, strong_candidate, python_job)
    assert isinstance(ext, MatchExtraction)
    # Omitted ids default to the real candidate/job (the extract() wrapper then pins them hard).
    assert ext.candidate_id == strong_candidate.candidate_id
    assert ext.job_id == python_job.job_id
