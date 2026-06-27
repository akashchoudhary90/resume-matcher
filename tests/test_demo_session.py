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


def test_dedupe_identical_uploads():
    # Byte-identical résumés (same file dropped twice) are scored once; the count surfaces in the UI.
    store = SessionStore(ttl_seconds=600)
    same = b"Python and SQL developer with REST experience. " * 4
    sess = run_demo(
        store=store, required_skills=["python", "sql"],
        files=[("a.txt", same), ("b.txt", same), ("c.txt", b"Java and Docker developer. " * 4)],
    )
    assert sess.duplicates_removed == 1
    assert sess.n_resumes == 2
    assert sess.to_dict()["duplicates_removed"] == 1


def test_leaderboard_sort_is_stable_on_ties():
    # Equal-scoring résumés must order deterministically (by label) so the leaderboard never reshuffles.
    store = SessionStore(ttl_seconds=600)
    body = b"Python developer. " * 4
    sess = run_demo(
        store=store, required_skills=["python"],
        files=[("zeta.txt", body + b" z"), ("alpha.txt", body + b" a"), ("mike.txt", body + b" m")],
    )
    scores = [r["fit_score"] for r in sess.results]
    labels = [r["label"] for r in sess.results]
    if len(set(scores)) == 1:  # all tied -> labels must be ascending
        assert labels == sorted(labels)


def test_gap_view_extra_skills():
    # The gap view shows skills the candidate has that the job didn't ask for.
    store = SessionStore(ttl_seconds=600)
    sess = run_demo(
        store=store, required_skills=["python"],
        files=[("R.txt", b"Python developer who also knows Docker and Tableau. " * 4)],
    )
    extra = sess.results[0]["extra_skills"]
    assert "Docker" in extra and "Tableau" in extra and "Python" not in extra


def test_candidate_from_text_redacts_contacts_keeps_name():
    # The file-direct/transcription helper must strip contact identifiers but keep the body + name.
    from resume_matcher.api.demo import _candidate_from_text

    cand = _candidate_from_text(
        "R1", "Jane Doe\njane@example.com (416) 555-1212\nPython and SQL developer."
    )
    assert "jane@example.com" not in cand.text and "555" not in cand.text  # contact redacted
    assert "Jane" in cand.text  # applicant name kept (identifiable, by consent)
    assert "python" in cand.skills and "sql" in cand.skills  # body preserved


def test_demo_text_path_redacts_contacts_before_scoring(monkeypatch):
    # Contact identifiers must be stripped from the candidate text BEFORE it reaches the matching
    # engine (and any LLM) — keeping only the body + name. We spy on the adapter's input to prove it.
    from resume_matcher.inference.adapters import mock as mock_mod

    seen: dict[str, str] = {}
    orig = mock_mod.MockAdapter._extract

    def spy(self, candidate, job):
        seen["text"] = candidate.text
        return orig(self, candidate, job)

    monkeypatch.setattr(mock_mod.MockAdapter, "_extract", spy)
    store = SessionStore(ttl_seconds=600)
    run_demo(
        store=store, required_skills=["python"],
        files=[("a.txt", b"Jane Doe\njane@example.com (416) 555-1212\nPython developer. " * 2)],
    )
    text = seen["text"]
    assert "jane@example.com" not in text and "555" not in text  # contact identifiers redacted
    assert "Jane" in text and "Python developer" in text  # name + body kept


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


def test_session_store_and_cache_concurrency():
    # #20: hammer create/get/delete + the shared extraction cache from many threads; no crash, no
    # corrupted invariants (active_count stays sane, store survives).
    import threading

    store = SessionStore(ttl_seconds=600, max_sessions=50)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for _ in range(12):
                s = run_demo(store=store, required_skills=["python"],
                             files=[(f"r{i}.txt", b"Python and SQL developer with experience. " * 4)])
                store.get(s.session_id)
                store.delete(s.session_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert 0 <= store.active_count() <= 50


def test_max_sessions_evicts_oldest():
    store = SessionStore(ttl_seconds=600, max_sessions=2)
    a = run_demo(store=store, required_skills=["python"], files=_files(1))
    a.last_seen -= 5  # make 'a' the least-recently-seen
    run_demo(store=store, required_skills=["python"], files=_files(1))
    run_demo(store=store, required_skills=["python"], files=_files(1))  # triggers eviction
    assert store.active_count() <= 2
    assert store.get(a.session_id) is None  # oldest evicted


def test_extract_cache_evicts_only_lru_not_whole_cache(monkeypatch):
    # Overflow must drop ONLY the least-recently-used entry, not clear() the whole consistency cache.
    from resume_matcher.api import demo as demo_mod

    monkeypatch.setattr(demo_mod, "CACHE_MAX", 3)
    demo_mod._EXTRACT_CACHE.clear()
    try:
        for i in range(3):
            demo_mod._cache_put(f"k{i}", i)
        assert demo_mod._cache_get("k0") == 0  # touch k0 -> most-recently-used
        demo_mod._cache_put("k3", 3)           # overflow: evicts the LRU (k1)
        assert len(demo_mod._EXTRACT_CACHE) == 3
        assert demo_mod._cache_get("k1") is None   # evicted
        assert demo_mod._cache_get("k0") == 0      # survived (recently touched)
        assert demo_mod._cache_get("k2") == 2
        assert demo_mod._cache_get("k3") == 3
    finally:
        demo_mod._EXTRACT_CACHE.clear()
