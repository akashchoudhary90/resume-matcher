"""DB-backed job queue: the platform's async engine (docs/PLATFORM.md, Slice B).

Replaces the demo's in-RAM thread-spawn for PLATFORM work: a `jobs` row is the source of truth, so
a 202 placeholder survives restarts (stale `running` rows are re-queued on boot), a poison job
retries with backoff instead of wedging a thread, and `dedupe_key` makes double-submits idempotent.
The claim is an atomic single-statement UPDATE (the SQLite equivalent of SKIP LOCKED; the Postgres
port keeps the same store contract).

Handlers are plain callables `handler(payload: dict, progress) -> dict` registered by kind in
HANDLERS (api/platform.py registers extract_posting). The handler's return value is stored as the
job's result_json — exactly what the generic GET /api/jobs/{id} poll route serves.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from contextlib import closing
from typing import Callable

from ..config import env_int
from ..stores.db import connect, migrate, platform_db_path

_log = logging.getLogger("resume_matcher.workers")

# kind -> handler(payload, progress) -> result dict. Registered at import time by route modules.
HANDLERS: dict[str, Callable[[dict, Callable], dict]] = {}


def register_handler(kind: str):
    """Decorator: @register_handler('extract_posting')."""

    def _wrap(fn):
        HANDLERS[kind] = fn
        return fn

    return _wrap


class JobStore:
    """Thin DAO over the jobs table. A new connection per call (thread-safe under the pool)."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        *,
        owner_user_id: int | None = None,
        total: int = 0,
        dedupe_key: str | None = None,
    ) -> str:
        """Queue a job; an existing PENDING job with the same dedupe_key is returned instead
        (coalesces concurrent double-submits). A dedupe job that has already FINISHED (done/error)
        is superseded by a fresh run — otherwise a constant dedupe_key (e.g. build_edges:{school})
        would make the job run exactly once ever and silently stop after its first completion."""
        job_id = secrets.token_urlsafe(12)
        with closing(self._conn()) as conn:
            try:
                conn.execute(
                    "INSERT INTO jobs(id, kind, owner_user_id, progress_total, payload_json, "
                    "dedupe_key, created_at) VALUES(?,?,?,?,?,?,?)",
                    (job_id, kind, owner_user_id, total, json.dumps(payload or {}),
                     dedupe_key, time.time()),
                )
                conn.commit()
                return job_id
            except Exception:
                if dedupe_key is None:
                    raise
                row = conn.execute(
                    "SELECT id, status FROM jobs WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                if row is None:  # not a dedupe collision after all
                    raise
                if row["status"] in ("done", "error"):
                    # the prior run is finished — replace it so this submit actually re-runs
                    conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                    conn.execute(
                        "INSERT INTO jobs(id, kind, owner_user_id, progress_total, payload_json, "
                        "dedupe_key, created_at) VALUES(?,?,?,?,?,?,?)",
                        (job_id, kind, owner_user_id, total, json.dumps(payload or {}),
                         dedupe_key, time.time()),
                    )
                    conn.commit()
                    return job_id
                return row["id"]  # still queued/running -> idempotent

    def get(self, job_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json") or "{}")
        raw = job.pop("result_json")
        job["result"] = json.loads(raw) if raw else None
        return job

    def claim(self, worker_name: str) -> dict | None:
        """Atomically claim the oldest due queued job (status flip + attempt count in one UPDATE)."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute(
                "UPDATE jobs SET status='running', locked_by=?, started_at=?, "
                "attempts=attempts+1 WHERE id = (SELECT id FROM jobs WHERE status='queued' "
                "AND run_after<=? ORDER BY created_at LIMIT 1) RETURNING *",
                (worker_name, now, now),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json") or "{}")
        return job

    def update_progress(self, job_id: str, done: int, total: int | None = None) -> None:
        with closing(self._conn()) as conn:
            if total is None:
                conn.execute("UPDATE jobs SET progress_done=? WHERE id=?", (done, job_id))
            else:
                conn.execute("UPDATE jobs SET progress_done=?, progress_total=? WHERE id=?",
                             (done, total, job_id))
            conn.commit()

    def complete(self, job_id: str, result: dict | None) -> None:
        with closing(self._conn()) as conn:
            # Scrub the request payload on completion: it can hold PII (e.g. an uploaded
            # Connections.csv, raw JD text) that must not persist in the jobs table after the
            # work is done. The result_json is the durable, de-identified output.
            conn.execute(
                "UPDATE jobs SET status='done', result_json=?, payload_json='{}', error=NULL, "
                "finished_at=? WHERE id=? AND status='running'",
                (json.dumps(result or {}), time.time(), job_id),
            )
            conn.commit()

    def fail(self, job_id: str, error: str, *, max_attempts: int, backoff_sec: float) -> None:
        """Retry with backoff until max_attempts, then park as a terminal error."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return
            if row["attempts"] >= max_attempts:
                # terminal failure: no more retries need the payload, so scrub its PII too.
                conn.execute(
                    "UPDATE jobs SET status='error', error=?, payload_json='{}', finished_at=? "
                    "WHERE id=?",
                    ((error or "job failed")[:2000], time.time(), job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='queued', locked_by=NULL, error=?, run_after=? "
                    "WHERE id=?",
                    ((error or "job failed")[:2000], time.time() + backoff_sec, job_id),
                )
            conn.commit()

    def requeue_stale(self, older_than_sec: float = 600.0) -> int:
        """Boot-time recovery: a `running` row older than the threshold belonged to a dead process
        (the '202 placeholder dies on restart' hole, closed)."""
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='queued', locked_by=NULL WHERE status='running' "
                "AND started_at < ?",
                (time.time() - older_than_sec,),
            )
            conn.commit()
            return cur.rowcount


class WorkerPool:
    """Small daemon-thread pool polling the jobs table (the SessionStore-contract successor)."""

    def __init__(
        self,
        store: JobStore,
        handlers: dict[str, Callable] | None = None,
        *,
        workers: int | None = None,
        poll_interval: float = 0.5,
        max_attempts: int | None = None,
    ) -> None:
        self.store = store
        self.handlers = HANDLERS if handlers is None else handlers
        self.workers = workers or max(1, env_int("RM_PLATFORM_WORKERS", 2))
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts or max(1, env_int("RM_JOB_MAX_ATTEMPTS", 3))
        self.backoff = lambda attempts: min(2.0 ** attempts, 60.0)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        requeued = self.store.requeue_stale()
        if requeued:
            _log.info("re-queued %d stale running job(s) from a previous process", requeued)
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, args=(f"worker-{i}",),
                                 name=f"rm-job-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim(name)
            except Exception:  # noqa: BLE001 - a transient DB error must never kill the worker
                _log.warning("job claim failed", exc_info=True)
                job = None
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            self._run_one(job)

    def _run_one(self, job: dict) -> None:
        handler = self.handlers.get(job["kind"])
        if handler is None:
            # Unknown kind is permanent — retrying can't grow a handler.
            self.store.fail(job["id"], f"no handler for job kind {job['kind']!r}",
                            max_attempts=0, backoff_sec=0)
            return

        def progress(done: int, total: int | None = None) -> None:
            self.store.update_progress(job["id"], done, total)

        try:
            result = handler(job["payload"], progress)
        except Exception as exc:  # noqa: BLE001 - handler errors become job retries/errors
            _log.warning("job %s (%s) failed on attempt %d: %s",
                         job["id"], job["kind"], job["attempts"], exc)
            self.store.fail(job["id"], str(exc), max_attempts=self.max_attempts,
                            backoff_sec=self.backoff(job["attempts"]))
            return
        self.store.complete(job["id"], result)


# ---- shared instances (path-keyed like accounts.get_account_store) ------------------------------
_STORES: dict[str, JobStore] = {}
_POOLS: dict[str, WorkerPool] = {}
_LOCK = threading.Lock()


def get_job_store() -> JobStore:
    path = platform_db_path()
    with _LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = JobStore(path)
        return store


def start_worker_pool() -> WorkerPool:
    """Start (once per DB path) the pool over the shared HANDLERS registry."""
    path = platform_db_path()
    with _LOCK:
        pool = _POOLS.get(path)
        if pool is None:
            pool = _POOLS[path] = WorkerPool(JobStore(path))
            pool.start()
        return pool
