"""Ephemeral demo flow: in-memory only, drop-after-scoring, TTL + explicit delete, no disk writes.

These run on core deps only (no FastAPI needed) — they exercise the store and run_demo directly.
"""
from pathlib import Path

import pytest

from resume_matcher.api.demo import DemoError, SessionStore, run_demo

ROOT = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}


def _snapshot(root: Path) -> set[str]:
    out: set[str] = set()
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix != ".pyc":
            out.add(str(p.relative_to(root)))
    return out


def _files(n=2):
    base = [
        b"Jane Doe jane@example.com (416) 555-1212\nExperienced Python developer. Strong SQL. Built REST APIs. Bachelor of Science. 3 years experience. " * 3,
        b"John Q\nSkilled in Java, Docker and Python. Worked with React. Master of Engineering. " * 3,
        b"Pat\nData analysis with Excel and SQL. Diploma. " * 3,
    ]
    return [(f"resume_{i}.txt", base[i % len(base)]) for i in range(n)]


def test_run_demo_scores_and_drops_text():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store,
        job_text="Python developer with SQL and Docker. React a plus. Bachelor required.",
        title="Backend Dev", employer="Acme",
        required_skills=["python", "sql", "docker"], preferred_skills=["react"],
        min_education="bachelor",
        files=_files(2),
    )
    assert sess.n_resumes == 2
    assert len(sess.results) == 2
    # Raw resume text is NOT retained anywhere on the session object.
    assert not hasattr(sess, "text")
    blob = str(sess.to_dict())
    assert "jane@example.com" not in blob  # PII redacted/dropped
    assert "555" not in blob
    d = sess.to_dict()
    assert d["privacy"]["stored_on_disk"] is False
    assert d["privacy"]["raw_text_retained"] is False
    # Every result carries an auditable, reconciling breakdown.
    for r in sess.results:
        ex = r["explanation"]
        assert round(ex["subtotal"] * ex["education_factor"], 1) == r["fit_score"]


def test_results_labelled_by_filename_with_candidate_meta():
    store = SessionStore(ttl_seconds=600)
    files = [
        ("Jane Doe - Engineer.txt",
         b"Senior Python developer. 5 years experience. Strong SQL. Bachelor of Science. " * 3),
    ]
    sess = run_demo(store=store, required_skills=["python", "sql"], files=files)
    row = sess.results[0]
    assert row["label"] == "Jane Doe - Engineer"  # filename used as identifiable label
    assert row["education_level"] == "bachelor"
    assert row["years_experience"] == 5.0
    assert row["skills_found"] >= 2


def test_run_demo_writes_nothing_to_disk():
    before = _snapshot(ROOT)
    store = SessionStore(ttl_seconds=600)
    run_demo(
        store=store, job_text="Python and SQL.", required_skills=["python", "sql"],
        files=_files(3),
    )
    after = _snapshot(ROOT)
    assert after == before, f"demo wrote files to disk: {sorted(after - before)}"


def test_autodetects_job_skills_from_posting():
    # No explicit skills tagged — they should be auto-detected from the pasted posting.
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store,
        job_text="We need a developer with Python, SQL and Docker. Bachelor required.",
        files=[("a.txt", b"Python and SQL and Docker developer. " * 5)],
    )
    assert sess.job["required_skills"]  # auto-detected, non-empty
    assert sess.results[0]["fit_score"] > 0


def test_no_job_skills_refused_not_zero():
    # A posting with no recognizable skills (and none tagged) must error clearly, not score everyone 0.
    store = SessionStore(ttl_seconds=600)
    with pytest.raises(DemoError):
        run_demo(
            store=store,
            job_text="We are a great company with a fun culture and free snacks.",
            files=[("a.txt", b"Python developer. " * 5)],
        )


def test_too_many_resumes_rejected():
    store = SessionStore()
    with pytest.raises(DemoError):
        run_demo(store=store, required_skills=["python"], files=_files(11))


def test_no_readable_text_rejected():
    store = SessionStore()
    with pytest.raises(DemoError):
        run_demo(store=store, required_skills=["python"], files=[("empty.txt", b"   ")])


def test_explicit_delete_purges_session():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(store=store, required_skills=["python"], files=_files(1))
    sid = sess.session_id
    assert store.get(sid) is not None
    assert store.delete(sid) is True
    assert store.get(sid) is None
    assert store.delete(sid) is False  # idempotent


def test_idle_ttl_expiry():
    store = SessionStore(ttl_seconds=1)
    sess = run_demo(store=store, required_skills=["python"], files=_files(1))
    sid = sess.session_id
    store._sessions[sid].last_seen -= 10  # simulate idle past the TTL
    assert store.get(sid) is None
    assert store.active_count() == 0


def test_sweep_removes_expired():
    store = SessionStore(ttl_seconds=1)
    s1 = run_demo(store=store, required_skills=["python"], files=_files(1))
    store._sessions[s1.session_id].last_seen -= 10
    assert store.sweep() == 1
    assert store.active_count() == 0


def test_max_sessions_evicts_oldest():
    store = SessionStore(ttl_seconds=600, max_sessions=2)
    a = run_demo(store=store, required_skills=["python"], files=_files(1))
    a.last_seen -= 5  # make 'a' the least-recently-seen
    run_demo(store=store, required_skills=["python"], files=_files(1))
    run_demo(store=store, required_skills=["python"], files=_files(1))  # triggers eviction
    assert store.active_count() <= 2
    assert store.get(a.session_id) is None  # oldest evicted
