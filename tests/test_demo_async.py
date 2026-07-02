"""Async demo runs: 202-accept + background scoring + poll-until-done.

This contract is what lets a long NDR-AI batch (up to jobs×resumes LLM calls) survive real-world
plumbing: no browser, proxy, or container restart has to keep one HTTP request alive for the whole
run, and a finished run stays reachable via its session until the TTL.
"""
import threading
import time

import pytest

from resume_matcher.api import demo as demo_mod
from resume_matcher.api.demo import SessionStore, start_async_run


def _wait_leave_running(store, sid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sess = store.get(sid)
        if sess is None or sess.status != "running":
            return sess
        time.sleep(0.01)
    raise AssertionError("session never left 'running'")


def test_pending_session_shape_and_progress():
    store = SessionStore(ttl_seconds=600)
    sess = store.create_pending(mode="grid", total=6)
    d = sess.to_dict()
    assert d["status"] == "running" and d["mode"] == "grid" and d["error"] is None
    assert d["progress"] == {"done": 0, "total": 6}
    assert d["results"] == [] and d["grid"] is None
    store.update_progress(sess.session_id, done_inc=2)
    store.update_progress(sess.session_id, total=5)
    assert store.get(sess.session_id).progress == {"done": 2, "total": 5}


def test_start_async_run_completes_and_reports_progress():
    store = SessionStore(ttl_seconds=600)
    finishes = []
    pending = start_async_run(
        store=store, kind="single", total_estimate=2, on_finish=finishes.append,
        required_skills=["python"],
        files=[("a.txt", b"Python developer. " * 4), ("b.txt", b"Java developer. " * 4)],
    )
    assert pending.status == "running"
    sess = _wait_leave_running(store, pending.session_id)
    assert sess.status == "done"
    assert sess.progress["done"] == sess.progress["total"] == 2
    assert sess.n_resumes == 2 and len(sess.results) == 2
    assert finishes == [True]  # on_finish ran (exactly once) before the session flipped to done


def test_start_async_run_failure_surfaces_error():
    store = SessionStore(ttl_seconds=600)
    finishes = []
    pending = start_async_run(
        store=store, kind="single", total_estimate=1, on_finish=finishes.append,
        required_skills=["python"], files=[("empty.txt", b"   ")],
    )
    sess = _wait_leave_running(store, pending.session_id)
    assert sess.status == "error"
    assert "could be scored" in (sess.error or "")
    assert finishes == [False]  # a failed run is not charged
    assert sess.results == [] and sess.grid is None


def test_start_async_run_rejects_unknown_kind():
    with pytest.raises(ValueError):
        start_async_run(store=SessionStore(), kind="nope", total_estimate=1, files=[])


def test_delete_mid_run_discards_results(monkeypatch):
    # "Delete my data now" while scoring runs: the finished results must be dropped, not resurrected
    # into the deleted session.
    release = threading.Event()
    real = demo_mod.run_demo

    def slow_run(**kwargs):
        assert release.wait(5)
        return real(**kwargs)

    monkeypatch.setattr(demo_mod, "run_demo", slow_run)
    store = SessionStore(ttl_seconds=600)
    finishes = []
    pending = start_async_run(
        store=store, kind="single", total_estimate=1, on_finish=finishes.append,
        required_skills=["python"], files=[("a.txt", b"Python developer. " * 4)],
    )
    assert store.delete(pending.session_id) is True
    release.set()
    deadline = time.time() + 5
    while not finishes and time.time() < deadline:
        time.sleep(0.01)
    assert finishes == [True]                       # the compute happened (and is charged)...
    assert store.get(pending.session_id) is None    # ...but nothing was stored or resurrected


def test_grid_files_refines_progress_total():
    # The 202 carries a pre-parse estimate; once the readable JDs are known the total is corrected.
    store = SessionStore(ttl_seconds=600)
    pending = start_async_run(
        store=store, kind="grid_files", total_estimate=99,
        jd_files=[("Dev.txt", b"Python and SQL developer.")],
        resume_files=[("Alice.txt", b"Python SQL developer. " * 4),
                      ("Bob.txt", b"Excel analyst. " * 4)],
    )
    sess = _wait_leave_running(store, pending.session_id)
    assert sess.status == "done"
    assert sess.progress == {"done": 2, "total": 2}  # 1 role x 2 resumes, not the 99 estimate


def test_eviction_prefers_finished_sessions_over_running():
    # A full store must not sacrifice an in-flight run (its poller would 404 mid-run and the
    # worker's results would be discarded) while a finished session can be evicted instead.
    store = SessionStore(ttl_seconds=600, max_sessions=2)
    running = store.create_pending(mode="single", total=1)
    running.last_seen -= 100  # oldest by far — the naive LRU victim
    done = store.create_pending(mode="single", total=1)
    done.status = "done"
    store.create_pending(mode="single", total=1)  # triggers eviction at capacity
    assert store.get(running.session_id) is not None   # in-flight run survived
    assert store.get(done.session_id) is None          # the finished session was evicted instead


def test_accepted_response_says_running_even_if_worker_already_failed():
    # Fast-failure race: a batch that fails validation inside the worker can flip the session to
    # 'error' BEFORE the 202 serializes. The 202 body must still say 'running' (it carries no error
    # message or results), so the client always reaches the poll endpoint — the source of truth.
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    import io

    from conftest import finish_demo_run
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app

    client = TestClient(create_app())
    files = [  # whitespace-only JD -> DemoError raised almost instantly inside the worker
        ("jds", ("bad.txt", io.BytesIO(b"   "), "text/plain")),
        ("resumes", ("Alice.txt", io.BytesIO(b"Python developer. " * 4), "text/plain")),
    ]
    accepted = client.post("/api/demo/run-grid-files", files=files)
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["status"] == "running"          # snapshot, never the live (possibly-failed) state
    assert body["progress"]["done"] == 0
    final = finish_demo_run(client, accepted)
    assert final.json()["status"] == "error"    # the poll carries the real outcome + message
    assert "readable" in final.json()["error"]


def test_exports_blocked_while_running():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    from resume_matcher.api.demo import get_demo_store

    client = TestClient(create_app())
    pending = get_demo_store().create_pending(mode="single", total=1)
    sid = pending.session_id
    try:
        poll = client.get(f"/api/demo/session/{sid}")
        assert poll.status_code == 200 and poll.json()["status"] == "running"
        # Results don't exist yet — exporting/saving a running session must refuse, not emit empties.
        assert client.get(f"/api/demo/session/{sid}/export.csv").status_code == 409
        assert client.get(f"/api/demo/session/{sid}/export.json").status_code == 409
        assert client.get(f"/api/demo/session/{sid}/defense-file.json").status_code == 409
    finally:
        get_demo_store().delete(sid)
