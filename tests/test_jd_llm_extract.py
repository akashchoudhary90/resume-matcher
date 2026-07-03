"""JD-side requirement extraction: NDR AI reads the posting; keyword scan is the fallback.

Also covers the taxonomy fix that motivated it: bare "http"/"https" matched every URL printed in a
posting and polluted auto-detected requirements with a junk skill.
"""
import pytest

from resume_matcher.api import demo as demo_mod
from resume_matcher.api.demo import SessionStore, _llm_job_requirements, _skill_ids_from_names, run_demo
from resume_matcher.inference.adapters import claude_cli as claude_cli_mod
from resume_matcher.ingestion.job_posting import detect_job_skills
from resume_matcher.matching.taxonomy import normalize_skills


@pytest.fixture(autouse=True)
def _fresh_cache():
    demo_mod._EXTRACT_CACHE.clear()
    yield
    demo_mod._EXTRACT_CACHE.clear()


# ---- taxonomy: URLs are not skills --------------------------------------------------------------

def test_urls_no_longer_detected_as_skills():
    text = "Apply at https://jobs.example.com/apply or read http://example.com/about. Python needed."
    ids = detect_job_skills(text)
    assert "https" not in ids and "http" not in ids
    assert "python" in ids


def test_protocol_skills_still_reachable_via_qualified_aliases():
    assert normalize_skills("Implemented HTTP over TLS termination") == ["https"]


# ---- name -> id resolution ----------------------------------------------------------------------

def test_skill_ids_resolve_via_taxonomy_then_slug():
    ids = _skill_ids_from_names(["Python", "Zxqvbn Frobnication", "Python", "", 42])
    assert "python" in ids                       # taxonomy surface inside the name wins
    assert "zxqvbn_frobnication" in ids          # unknown to the taxonomy -> conservative slug
    assert ids.count("python") == 1              # deduped
    assert len(ids) == 2                         # junk entries dropped

    # A name CONTAINING a taxonomy surface resolves to the canonical id (synonyms collapse).
    assert "auditing" in _skill_ids_from_names(["Frobnication Auditing"])


# ---- _llm_job_requirements: validation, caching, fallbacks --------------------------------------

def _arm(monkeypatch, payload, calls=None):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: True)
    # Hermetic: _run_cli re-checks the environment itself, so on a box with the `claude` CLI and a
    # token, the resume-side extraction would otherwise spawn REAL subprocess calls. Force the
    # fail-quiet-to-mock path deterministically.
    def _no_cli(*a, **k):
        raise claude_cli_mod.InferenceError("hermetic test — no real CLI")
    monkeypatch.setattr(claude_cli_mod, "_run_cli", _no_cli)

    def fake_extract(job_text, title=""):
        if calls is not None:
            calls.append((job_text, title))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(claude_cli_mod, "extract_job_requirements", fake_extract)


def test_llm_requirements_validated_and_clamped(monkeypatch):
    _arm(monkeypatch, {
        "required_skills": ["Python", "SQL", "Communication"],
        "preferred_skills": ["Docker", "Python"],          # overlap with required is dropped
        "must_have_skills": ["Python", "Quantum Sorcery"],  # must-haves must be in required
        "min_education": "Bachelor",
        "min_years": 200,                                    # implausible -> dropped
    })
    out = _llm_job_requirements("We build things.", title="Dev")
    assert out["required_skills"] == ["python", "sql", "communication"]
    assert out["preferred_skills"] == ["docker"]
    assert out["must_have_skills"] == ["python"]
    assert out["min_education"] == "bachelor"
    assert out["min_years"] is None


def test_llm_requirements_cached_per_posting(monkeypatch):
    calls = []
    _arm(monkeypatch, {"required_skills": ["Python"]}, calls)
    a = _llm_job_requirements("Posting text A.")
    b = _llm_job_requirements("Posting text A.")
    assert a == b and len(calls) == 1            # second call served from the extraction cache


def test_llm_requirements_cache_keyed_by_title_too(monkeypatch):
    # The title steers the extraction (it's in the prompt), so the same posting text under a
    # DIFFERENT title must be a cache miss — not served the first title's requirement list.
    calls = []
    _arm(monkeypatch, {"required_skills": ["Python"]}, calls)
    _llm_job_requirements("Same posting text.", title="Senior Engineer")
    _llm_job_requirements("Same posting text.", title="Junior Intern")
    assert len(calls) == 2
    _llm_job_requirements("Same posting text.", title="  senior   ENGINEER ")  # normalized -> hit
    assert len(calls) == 2


def test_llm_requirements_negative_result_is_cached(monkeypatch):
    # An extraction that succeeds but yields nothing usable is cached (as a negative), so a re-run
    # of the same posting falls back to keywords instantly instead of re-paying the LLM call.
    calls = []
    _arm(monkeypatch, {"required_skills": [], "preferred_skills": []}, calls)
    assert _llm_job_requirements("Vague posting.") is None
    assert _llm_job_requirements("Vague posting.") is None
    assert len(calls) == 1


def test_skill_ids_keep_unicode_names():
    # Accented/CJK skill names must survive as matchable ids, not ASCII-mangled stubs.
    ids = _skill_ids_from_names(["Résumé Writing", "机器学习"])
    assert "résumé_writing" in ids
    assert "机器学习" in ids


def test_llm_requirements_unavailable_or_failing_returns_none(monkeypatch):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: False)
    assert _llm_job_requirements("Anything.") is None

    _arm(monkeypatch, RuntimeError("CLI exploded"))
    assert _llm_job_requirements("Anything else.") is None

    _arm(monkeypatch, {"required_skills": [], "preferred_skills": []})
    assert _llm_job_requirements("Empty extraction.") is None  # nothing usable -> keyword fallback


# ---- run_demo wiring ----------------------------------------------------------------------------

_RESUME = [("Alice.txt", b"Alice. Python and SQL developer, writes financial reports. " * 4)]


def test_run_demo_uses_llm_requirements_when_engine_active(monkeypatch):
    _arm(monkeypatch, {
        "required_skills": ["Python", "Financial Reporting"],
        "preferred_skills": ["SQL"],
        "min_education": "bachelor",
        "min_years": 3,
    })
    sess = run_demo(store=SessionStore(ttl_seconds=600), backend="claude_cli",
                    job_text="A posting that names no skills literally.", files=_RESUME)
    assert sess.job["skills_source"] == "ndr_ai"
    assert {s["id"] for s in sess.job["required_skills"]} == {"python", "financial_reporting"}
    assert {s["id"] for s in sess.job["preferred_skills"]} == {"sql"}
    assert sess.job["min_education"] == "bachelor"
    assert sess.job["min_years"] == 3
    # Resume-side extraction fail-quiets to the deterministic engine offline; scoring still works.
    assert sess.results and sess.results[0]["fit_score"] > 0


def test_run_demo_falls_back_to_keyword_scan_on_llm_failure(monkeypatch):
    _arm(monkeypatch, RuntimeError("boom"))
    sess = run_demo(store=SessionStore(ttl_seconds=600), backend="claude_cli",
                    job_text="Python developer with SQL.", files=_RESUME)
    assert sess.job["skills_source"] == "keyword"
    assert {s["id"] for s in sess.job["required_skills"]} >= {"python", "sql"}


def test_run_demo_never_overrides_user_tagged_skills(monkeypatch):
    calls = []
    _arm(monkeypatch, {"required_skills": ["Basket Weaving"]}, calls)
    sess = run_demo(store=SessionStore(ttl_seconds=600), backend="claude_cli",
                    job_text="Python developer.", required_skills=["python"], files=_RESUME)
    assert sess.job["skills_source"] == "user"
    assert calls == []                            # the model is not even consulted
    assert {s["id"] for s in sess.job["required_skills"]} == {"python"}


def test_run_demo_mock_engine_keeps_keyword_path(monkeypatch):
    calls = []
    _arm(monkeypatch, {"required_skills": ["Python"]}, calls)
    sess = run_demo(store=SessionStore(ttl_seconds=600), backend="mock",
                    job_text="Python developer with SQL.", files=_RESUME)
    assert sess.job["skills_source"] == "keyword"
    assert calls == []                            # LLM JD extraction is claude-engine-only
