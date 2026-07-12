"""P0→P5 orchestrator (ingestion/posting_extract.py): hermetic LLM arming (the test_jd_llm_extract
_arm pattern), fail-open, caching, injection posture, multi-role contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from resume_matcher.inference.adapters import claude_cli as claude_cli_mod
from resume_matcher.inference.posting_schema import FieldStatus
from resume_matcher.ingestion import posting_extract as pe
from resume_matcher.ingestion.posting_extract import PostingExtractError, extract_posting_draft

FIXTURES = Path(__file__).parent / "fixtures" / "jds"
CLEAN = (FIXTURES / "clean_en.txt").read_text(encoding="utf-8")
INJECTED = (FIXTURES / "injection.txt").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache():
    pe._CACHE.clear()
    yield
    pe._CACHE.clear()


def _arm(monkeypatch, payload, calls=None):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: True)

    def fake_extract(job_text, title="", only_role=""):
        if calls is not None:
            calls.append((job_text, title, only_role))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(claude_cli_mod, "extract_posting", fake_extract)


def test_deterministic_only_draft_when_llm_absent(monkeypatch):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: False)
    draft = extract_posting_draft(text=CLEAN, backend="claude_cli")
    assert "llm_unavailable" in draft.extraction_meta["flags"]
    assert draft.pay.value["min"] == 24.0                      # regexes still filled the form
    assert draft.application_deadline.value == "2027-03-15"
    assert {s.skill_id for s in draft.skills} >= {"python", "sql"}
    assert "kafka" not in {s.skill_id for s in draft.skills}   # section scoping survived


def test_llm_failure_fails_open_and_is_not_cached(monkeypatch):
    calls = []
    _arm(monkeypatch, RuntimeError("CLI exploded"), calls)
    draft = extract_posting_draft(text=CLEAN, backend="claude_cli")
    assert "llm_unavailable" in draft.extraction_meta["flags"]
    extract_posting_draft(text=CLEAN, backend="claude_cli")
    assert len(calls) == 2                                     # failures retry (not cached)


def test_llm_result_merges_and_caches(monkeypatch):
    calls = []
    _arm(monkeypatch, {
        "title": {"value": "Data Analyst Intern (Hybrid) — Summer 2027",
                  "quote": "Data Analyst Intern (Hybrid) — Summer 2027"},
        "employer_name": {"value": "Northwind Analytics", "quote": "Northwind Analytics"},
        "skills": [{"name": "Python", "bucket": "required", "kind": "named",
                    "quote": "2+ years of experience with Python for data analysis"}],
        "min_years": 2,
        "language": "en",
    }, calls)
    draft = extract_posting_draft(text=CLEAN, backend="claude_cli")
    assert draft.employer_name.value == "Northwind Analytics"
    py = next(s for s in draft.skills if s.skill_id == "python")
    assert py.status == FieldStatus.auto                       # dual-detector agreement
    assert draft.extraction_meta["model"]

    extract_posting_draft(text=CLEAN, backend="claude_cli")
    assert len(calls) == 1                                     # cache hit

    extract_posting_draft(text=CLEAN, backend="claude_cli", only_role="Other Role")
    assert len(calls) == 2                                     # role selection is a distinct key


def test_malformed_llm_payload_is_salvaged_per_field(monkeypatch):
    _arm(monkeypatch, {"title": {"value": "Data Analyst Intern", "quote": "Data Analyst Intern"},
                       "pay": "lots of money",                # invalid type -> dropped, not fatal
                       "skills": "Python"})                    # invalid type -> dropped
    draft = extract_posting_draft(text=CLEAN, backend="claude_cli")
    # The salvaged title survives as the LLM candidate (deterministic pre-fills on disagreement).
    assert draft.title.candidates[0]["value"] == "Data Analyst Intern"
    assert draft.pay.value["min"] == 24.0                      # deterministic pay survives


def test_injection_fixture_flags_and_nothing_red_auto_accepts(monkeypatch):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: False)
    draft = extract_posting_draft(text=INJECTED, backend="claude_cli")
    flags = draft.extraction_meta["flags"]
    assert any(f.startswith("injection_suspected") for f in flags)
    assert all(s.status != FieldStatus.auto for s in draft.skills)
    # the injected "$500 per hour" pay claim cannot auto-accept on a flagged document
    assert draft.pay.status != FieldStatus.auto


def test_multi_role_forces_title_decision(monkeypatch):
    _arm(monkeypatch, {"title": {"value": "Software Intern", "quote": "Software Intern"},
                       "multi_role_detected": True,
                       "other_role_titles": ["Data Analyst Intern"]})
    draft = extract_posting_draft(text="Software Intern\nRequirements\n- Python\n",
                                  backend="claude_cli")
    assert draft.title.status == FieldStatus.conflict
    assert draft.extraction_meta["other_role_titles"] == ["Data Analyst Intern"]


def test_empty_or_unreadable_input_raises_client_error():
    with pytest.raises(PostingExtractError):
        extract_posting_draft(text="   \n\n  ")


def test_file_bytes_path_uses_parser(monkeypatch):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: False)
    draft = extract_posting_draft(file_bytes=CLEAN.encode("utf-8"), filename="posting.txt",
                                  backend="claude_cli")
    assert draft.extraction_meta["source"] == "file"
    assert draft.pay.value["min"] == 24.0
