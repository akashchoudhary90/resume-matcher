"""DB-backed job queue (workers/runner.py): lifecycle, retry/backoff, idempotency, stale
recovery, and the generic poll route's owner/role visibility."""
from __future__ import annotations

import time

import pytest

from resume_matcher.stores.db import connect
from resume_matcher.workers.runner import JobStore, WorkerPool


def _wait_for(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _store(tmp_path) -> JobStore:
    return JobStore(str(tmp_path / "platform.db"))


def test_enqueue_run_done_with_progress(tmp_path):
    store = _store(tmp_path)

    def handler(payload, progress):
        progress(1, 2)
        progress(2)
        return {"echo": payload["x"]}

    pool = WorkerPool(store, {"echo": handler}, workers=1, poll_interval=0.01)
    pool.start()
    try:
        jid = store.enqueue("echo", {"x": 42}, owner_user_id=7)
        job = _wait_for(lambda: (j := store.get(jid)) and j["status"] == "done" and j)
        assert job["result"] == {"echo": 42}
        assert (job["progress_done"], job["progress_total"]) == (2, 2)
        assert job["owner_user_id"] == 7
    finally:
        pool.stop()


def test_failure_retries_then_parks_as_error(tmp_path):
    store = _store(tmp_path)
    calls = []

    def boom(payload, progress):
        calls.append(1)
        raise RuntimeError("kaput")

    pool = WorkerPool(store, {"boom": boom}, workers=1, poll_interval=0.01, max_attempts=2)
    pool.backoff = lambda attempts: 0.0  # no waiting between retries in tests
    pool.start()
    try:
        jid = store.enqueue("boom", {})
        job = _wait_for(lambda: (j := store.get(jid)) and j["status"] == "error" and j)
        assert len(calls) == 2                      # attempt 1 + retry, then parked
        assert "kaput" in job["error"]
    finally:
        pool.stop()


def test_unknown_kind_is_a_permanent_error(tmp_path):
    store = _store(tmp_path)
    pool = WorkerPool(store, {}, workers=1, poll_interval=0.01)
    pool.start()
    try:
        jid = store.enqueue("mystery", {})
        job = _wait_for(lambda: (j := store.get(jid)) and j["status"] == "error" and j)
        assert "no handler" in job["error"]
    finally:
        pool.stop()


def test_dedupe_key_is_idempotent(tmp_path):
    store = _store(tmp_path)
    a = store.enqueue("k", {"n": 1}, dedupe_key="same")
    b = store.enqueue("k", {"n": 2}, dedupe_key="same")
    assert a == b
    assert store.enqueue("k", {}, dedupe_key="other") != a


def test_dedupe_reruns_after_completion(tmp_path):
    """A FINISHED dedupe job must not block a fresh submit forever — otherwise a constant
    dedupe_key (build_edges:{school}) makes the job run exactly once ever."""
    store = _store(tmp_path)
    a = store.enqueue("k", {"n": 1}, dedupe_key="edges:1")
    store.claim("w")
    store.complete(a, {"ok": True})
    assert store.get(a)["status"] == "done"
    b = store.enqueue("k", {"n": 2}, dedupe_key="edges:1")
    assert b != a                       # a fresh run, not the stale completed job
    assert store.get(a) is None         # the finished one was superseded
    assert store.get(b)["status"] == "queued"


def test_completion_scrubs_payload_pii(tmp_path):
    """The request payload (which may hold an uploaded Connections.csv / raw JD text) must not
    persist after the job finishes — only the de-identified result is kept."""
    store = _store(tmp_path)
    jid = store.enqueue("resolve_network", {"csv_b64": "c2VjcmV0", "user_id": 1})
    store.claim("w")
    store.complete(jid, {"ok": True})
    job = store.get(jid)
    assert job["payload"] == {}          # scrubbed
    assert job["result"] == {"ok": True}  # result retained


def test_terminal_failure_scrubs_payload(tmp_path):
    store = _store(tmp_path)
    jid = store.enqueue("resolve_network", {"csv_b64": "c2VjcmV0"})
    store.claim("w")
    store.fail(jid, "boom", max_attempts=0, backoff_sec=0)   # no retries left -> terminal
    job = store.get(jid)
    assert job["status"] == "error" and job["payload"] == {}


def test_requeue_stale_recovers_dead_process_jobs(tmp_path):
    store = _store(tmp_path)
    jid = store.enqueue("k", {})
    claimed = store.claim("dead-worker")
    assert claimed["id"] == jid
    # simulate a crash long ago
    with connect(store.path) as conn:
        conn.execute("UPDATE jobs SET started_at=? WHERE id=?", (time.time() - 9999, jid))
        conn.commit()
    assert store.requeue_stale(older_than_sec=600) == 1
    assert store.get(jid)["status"] == "queued"


def test_poll_route_visibility(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from resume_matcher.api.accounts import get_account_store
    from resume_matcher.api.app import create_app
    from resume_matcher.workers.runner import get_job_store

    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    accounts = get_account_store()
    owner_tok, _ = accounts.register("owner@york.ca", "password123")
    other_tok, _ = accounts.register("other@york.ca", "password123")
    coord_tok, _ = accounts.create_user("c@york.ca", "password123", role="coordinator")
    owner = accounts.user_for_token(owner_tok)

    jid = get_job_store().enqueue("k", {}, owner_user_id=owner["id"])
    client = TestClient(create_app())

    assert client.get(f"/api/jobs/{jid}").status_code == 401          # anonymous
    client.cookies.set("rm_session", other_tok)
    assert client.get(f"/api/jobs/{jid}").status_code == 404          # not yours -> invisible
    client.cookies.set("rm_session", owner_tok)
    body = client.get(f"/api/jobs/{jid}")
    # The app's live pool may already have consumed the handlerless job — visibility is the point.
    assert body.status_code == 200 and body.json()["job_id"] == jid
    client.cookies.set("rm_session", coord_tok)
    assert client.get(f"/api/jobs/{jid}").status_code == 200          # coordinator sees all
    assert client.get("/api/jobs/nope").status_code == 404
