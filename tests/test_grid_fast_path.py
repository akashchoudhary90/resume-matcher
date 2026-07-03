"""Grid fast path: one batched NDR-AI extraction per resume (all roles), smart text/vision routing,
and the hidden-text scan surviving the move of text-layer PDFs off the vision path.

All hermetic: claude_cli._run_cli is force-failed so no real CLI can ever be spawned; the batched
extractor is stubbed at the module boundary, exactly like the JD-extraction tests.
"""
import threading

import pytest

from resume_matcher.api import demo as demo_mod
from resume_matcher.api.demo import SessionStore, _vision_candidate, run_demo_grid
from resume_matcher.inference.adapters import claude_cli as claude_cli_mod
from resume_matcher.inference.schema import Importance, MatchExtraction, MatchStatus, SkillEvidence


@pytest.fixture(autouse=True)
def _fresh_cache():
    demo_mod._EXTRACT_CACHE.clear()
    yield
    demo_mod._EXTRACT_CACHE.clear()


def _hermetic(monkeypatch):
    monkeypatch.setattr(claude_cli_mod, "available", lambda: True)

    def _no_cli(*a, **k):
        raise claude_cli_mod.InferenceError("hermetic test — no real CLI")

    monkeypatch.setattr(claude_cli_mod, "_run_cli", _no_cli)


def _evidence(job_id, cand, skill_id, quote):
    return MatchExtraction(
        candidate_id=cand, job_id=job_id,
        skill_matches=[SkillEvidence(skill_id=skill_id, skill_name=skill_id.title(),
                                     status=MatchStatus.match, importance=Importance.important,
                                     evidence_span=quote)],
        rationale=f"batched:{job_id}",
    )


_JOBS = [
    {"title": "Python Dev", "job_text": "Python developer with SQL."},
    {"title": "Analyst", "job_text": "Data analyst with Excel and SQL."},
]
_FILES = [
    ("Alice.txt", b"Alice is a python expert who also does sql and excel reporting. " * 3),
    ("Bob.txt", b"Bob writes sql and builds excel dashboards daily. " * 3),
]


def test_grid_prefetch_batches_one_call_per_resume(monkeypatch):
    _hermetic(monkeypatch)
    calls = []
    lock = threading.Lock()

    def fake_multi(cand, jobs):
        with lock:
            calls.append((cand.text[:20], sorted(j.job_id for j in jobs)))
        # Return a valid extraction per requested job, with a REAL verbatim quote so the ranker
        # verifies it — proving the prefetched extraction (marked via rationale) drove the score.
        quote = "python expert" if "python expert" in cand.text else "sql"
        return {j.job_id: _evidence(j.job_id, cand.candidate_id, "sql", quote) for j in jobs}

    monkeypatch.setattr(claude_cli_mod, "extract_multi", fake_multi)
    sess = run_demo_grid(store=SessionStore(ttl_seconds=600), jobs=_JOBS, files=_FILES,
                         backend="claude_cli")
    # One batched call per resume, each covering BOTH roles — not one call per cell.
    assert len(calls) == 2
    assert all(jids == ["J1", "J2"] for _, jids in calls)
    # The grid's cells were scored from the prefetched extractions (cache hits), not a fallback:
    # every cell's rationale carries the batch marker.
    rationales = {cell["result"]["rationale"]
                  for c in sess.to_dict()["grid"]["candidates"] for cell in c["cells"] if cell}
    assert rationales and all(r.startswith("batched:") for r in rationales)


def test_grid_prefetch_failure_falls_back_per_cell(monkeypatch):
    _hermetic(monkeypatch)

    def broken_multi(cand, jobs):
        raise RuntimeError("batched call exploded")

    monkeypatch.setattr(claude_cli_mod, "extract_multi", broken_multi)
    sess = run_demo_grid(store=SessionStore(ttl_seconds=600), jobs=_JOBS, files=_FILES,
                         backend="claude_cli")
    grid = sess.to_dict()["grid"]
    # Whole grid still completes (per-cell path; hermetic CLI -> deterministic engine fallback).
    assert len(grid["jobs"]) == 2 and len(grid["candidates"]) == 2
    for c in grid["candidates"]:
        assert all(cell is not None for cell in c["cells"])


def test_grid_prefetch_partial_answer_covers_only_returned_jobs(monkeypatch):
    _hermetic(monkeypatch)

    def partial_multi(cand, jobs):
        j = jobs[0]  # answers only the first requested job; the other must fall back cleanly
        return {j.job_id: _evidence(j.job_id, cand.candidate_id, "sql", "sql")}

    monkeypatch.setattr(claude_cli_mod, "extract_multi", partial_multi)
    sess = run_demo_grid(store=SessionStore(ttl_seconds=600), jobs=_JOBS, files=_FILES,
                         backend="claude_cli")
    grid = sess.to_dict()["grid"]
    assert len(grid["jobs"]) == 2
    for c in grid["candidates"]:
        assert all(cell is not None for cell in c["cells"])  # missing job filled by per-cell path


def test_grid_prefetch_disabled_by_kill_switch(monkeypatch):
    _hermetic(monkeypatch)
    calls = []
    monkeypatch.setattr(claude_cli_mod, "extract_multi",
                        lambda cand, jobs: calls.append(1) or {})
    monkeypatch.setattr(demo_mod, "BATCH_ROLES", False)
    run_demo_grid(store=SessionStore(ttl_seconds=600), jobs=_JOBS, files=_FILES,
                  backend="claude_cli")
    assert calls == []


def test_grid_prefetch_skipped_for_mock_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_cli_mod, "extract_multi",
                        lambda cand, jobs: calls.append(1) or {})
    run_demo_grid(store=SessionStore(ttl_seconds=600), jobs=_JOBS, files=_FILES, backend="mock")
    assert calls == []


# ---- smart text/vision routing ------------------------------------------------------------------

def test_vision_candidate_routing():
    # Text-layer PDFs skip vision; scans/images (no local text) keep it.
    pytest.importorskip("pdfplumber")
    # .txt is never a vision candidate regardless of content.
    use, _ = _vision_candidate("resume.txt", ".txt", b"plain text resume " * 50)
    assert use is False
    # An image is always a vision candidate (no text layer to extract locally).
    use, independent = _vision_candidate("scan.png", ".png", b"\x89PNG....")
    assert use is True and independent == ""
    # A "PDF" whose local parse yields nothing (corrupt/scan-like) is a vision candidate.
    use, independent = _vision_candidate("scan.pdf", ".pdf", b"%PDF-1.4 garbage")
    assert use is True and independent.strip() == ""


def test_extract_multi_parses_and_filters(monkeypatch):
    # extract_multi: valid per-job items are returned; wrong/unknown job_ids and garbage are not.
    from resume_matcher.ingestion.job_posting import build_job_spec
    from resume_matcher.inference.schema import CandidateProfile

    jobs = [build_job_spec(job_id="J1", title="A", required_skills=["python"]),
            build_job_spec(job_id="J2", title="B", required_skills=["sql"])]
    cand = CandidateProfile(candidate_id="C1", text="python and sql")
    raw = {
        "extractions": [
            {"candidate_id": "C1", "job_id": "J1", "skill_matches": [], "gaps": []},
            {"candidate_id": "C1", "job_id": "J9", "skill_matches": [], "gaps": []},  # unknown job
            "garbage",
            {"job_id": "J2", "skill_matches": "not-a-list"},                          # invalid
        ]
    }
    monkeypatch.setattr(claude_cli_mod, "_run_cli", lambda *a, **k: __import__("json").dumps(raw))
    out = claude_cli_mod.extract_multi(cand, jobs)
    assert set(out) == {"J1"}
    assert out["J1"].candidate_id == "C1"


def test_extract_multi_requires_extractions_array(monkeypatch):
    from resume_matcher.ingestion.job_posting import build_job_spec
    from resume_matcher.inference.schema import CandidateProfile

    monkeypatch.setattr(claude_cli_mod, "_run_cli", lambda *a, **k: '{"nope": 1}')
    with pytest.raises(claude_cli_mod.InferenceError):
        claude_cli_mod.extract_multi(
            CandidateProfile(candidate_id="C1", text="x"),
            [build_job_spec(job_id="J1", title="A", required_skills=["python"])])
