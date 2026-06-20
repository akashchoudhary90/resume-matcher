"""Claude-CLI backend (subscription, no API key) + the demo's fail-quiet fallback to mock.

These never invoke the real `claude` CLI — `_run_cli` / `available` are monkeypatched.
"""
import json

from resume_matcher.api.demo import SessionStore, run_demo
from resume_matcher.inference.adapters import claude_cli
from resume_matcher.inference.schema import CandidateProfile, JobSpec, MatchExtraction


def test_adapter_parses_cli_output(monkeypatch):
    payload = {
        "candidate_id": "ignored", "job_id": "ignored",
        "skill_matches": [
            {"skill_id": "python", "skill_name": "Python", "status": "match", "evidence_span": "Python"}
        ],
        "gaps": [], "rationale": "ok",
    }
    # CLI may wrap JSON in prose/fences — parse_extraction tolerates it.
    monkeypatch.setattr(claude_cli, "_run_cli", lambda prompt: "```json\n" + json.dumps(payload) + "\n```")
    cand = CandidateProfile(candidate_id="C1", text="Python developer.")
    job = JobSpec(job_id="J1", title="t", employer="e", required_skills=["python"])
    ext = claude_cli.ClaudeCliAdapter().extract(cand, job)
    assert isinstance(ext, MatchExtraction)
    assert ext.candidate_id == "C1" and ext.job_id == "J1"  # ids pinned by the base adapter
    assert ext.skill_matches[0].skill_id == "python"


def test_available_false_without_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert claude_cli.available() is False


def test_demo_falls_back_to_mock_when_claude_unavailable(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, backend="claude_cli", required_skills=["python"],
        files=[("a.txt", b"Python and SQL developer. " * 6)],
    )
    assert sess.engine == "mock"
    assert any("Claude backend unavailable" in w for w in sess.warnings)
    assert sess.results and sess.results[0]["fit_score"] >= 0


def test_demo_uses_claude_when_available(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    payload = {
        "candidate_id": "x", "job_id": "x",
        "skill_matches": [
            {"skill_id": "python", "skill_name": "Python", "status": "match", "evidence_span": "Python"}
        ],
        "gaps": [], "rationale": "",
    }
    monkeypatch.setattr(claude_cli, "_run_cli", lambda prompt: json.dumps(payload))
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, backend="claude_cli", required_skills=["python"],
        files=[("Jane.txt", b"Senior Python developer with broad experience. " * 6)],
    )
    assert sess.engine == "claude_cli"
    assert sess.to_dict()["engine"] == "claude_cli"
    assert sess.results[0]["fit_score"] == 100.0


def test_demo_claude_per_candidate_failure_falls_back(monkeypatch):
    # An LLM error on a single resume must not sink the batch — that candidate scores via mock.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(claude_cli, "available", lambda: True)

    def boom(prompt):
        raise RuntimeError("simulated CLI failure")

    monkeypatch.setattr(claude_cli, "_run_cli", boom)
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, backend="claude_cli", required_skills=["python"],
        files=[("a.txt", b"Python developer. " * 8)],
    )
    # engine is reported as claude_cli (it was selected), but the result still computed via fallback.
    assert sess.results and sess.results[0]["fit_score"] == 100.0
