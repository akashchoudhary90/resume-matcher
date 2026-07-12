"""Slice N: backend-agnostic JD extraction + the boundary-#3 redaction gate for non-local
backends (hermetic — backend callables are monkeypatched, nothing real is called)."""
from __future__ import annotations

from pathlib import Path

import pytest

from resume_matcher.ingestion import posting_extract as pe
from resume_matcher.ingestion.posting_extract import extract_posting_draft

FIXTURES = Path(__file__).parent / "fixtures" / "jds"
CLEAN = (FIXTURES / "clean_en.txt").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache():
    pe._CACHE.clear()
    yield
    pe._CACHE.clear()


def _arm_backend(monkeypatch, backend, payload, *, is_local=True, calls=None):
    def fake(text, title="", only_role=""):
        if calls is not None:
            calls.append(text)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setitem(pe._BACKEND_CALLS, backend, (fake, lambda: is_local))


def test_ollama_backend_merges_like_claude(monkeypatch):
    _arm_backend(monkeypatch, "ollama", {
        "employer_name": {"value": "Northwind Analytics", "quote": "Northwind Analytics"},
        "min_years": 2, "language": "en",
    })
    draft = extract_posting_draft(text=CLEAN, backend="ollama")
    assert draft.employer_name.value == "Northwind Analytics"
    assert draft.extraction_meta["model"].startswith("ollama:")
    assert "llm_unavailable" not in draft.extraction_meta["flags"]


def test_local_backend_sees_the_raw_jd(monkeypatch):
    calls = []
    _arm_backend(monkeypatch, "ollama", {"language": "en"}, is_local=True, calls=calls)
    extract_posting_draft(text=CLEAN, backend="ollama")
    assert "recruiting@northwind.example.com" in calls[0]   # local: nothing stripped


def test_non_local_backend_only_sees_redacted_jd(monkeypatch):
    calls = []
    _arm_backend(monkeypatch, "openai_compat", {"language": "en"}, is_local=False, calls=calls)
    draft = extract_posting_draft(text=CLEAN, backend="openai_compat")
    sent = calls[0]
    assert "recruiting@northwind.example.com" not in sent    # boundary #3: redacted copy
    assert "https://jobs.example.com" not in sent
    assert "[EMAIL]" in sent and "[URL]" in sent
    # ...and the form still has the contacts — P2 captured them deterministically first
    assert draft.application.value["email"] == "recruiting@northwind.example.com"


def test_unknown_backend_fails_open(monkeypatch):
    draft = extract_posting_draft(text=CLEAN, backend="carrier_pigeon")
    assert "llm_unavailable" in draft.extraction_meta["flags"]
    assert draft.pay.value["min"] == 24.0                    # deterministic draft intact


def test_backend_failure_fails_open(monkeypatch):
    _arm_backend(monkeypatch, "ollama", RuntimeError("model not pulled"))
    draft = extract_posting_draft(text=CLEAN, backend="ollama")
    assert "llm_unavailable" in draft.extraction_meta["flags"]
