"""FastAPI web application: JSON API + served coordinator dashboard.

Run locally:  python scripts/serve.py            then open http://127.0.0.1:8000
Deploy:       see DEPLOY.md (Docker + Caddy auto-HTTPS behind your subdomain)

The whole app is behind an admin sign-in page (auth.require_auth + /login) when RM_ADMIN_PASSWORD is set.
The package imports fine without FastAPI installed (`app` is None until requirements-extra is in).
API is the contract; the bundled HTML dashboard is a thin, replaceable client (plan §UI).

NOTE: this module intentionally does NOT use `from __future__ import annotations` — FastAPI must
introspect real annotation objects to wire request/Form parameters, and stringized annotations can
break that. The upload route parses the multipart form manually (request.form) so it can raise the
per-file size / count limits above Starlette's defaults and keep uploads in memory.
"""
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..config import env_flag
from ..ingestion.job_posting import parse_job_posting, skill_options
from ..ingestion.parser import SUPPORTED_EXTS
from ..inference.schema import CandidateProfile, JobSpec
from ..matching.evaluator import evaluate
from . import demo as demo_mod
from .accounts import AccountError, AccountStore, cookie_max_age
from .demo import DemoError, get_demo_store, run_demo
from .service import get_state

_STATIC = Path(__file__).with_name("static")
_SWEEPER_STARTED = False
_log = logging.getLogger("resume_matcher.api")


def _client_key(request) -> str:
    """Best-effort client identity for rate limiting: the first X-Forwarded-For hop (the demo sits
    behind Caddy) else the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class _RateLimiter:
    """Tiny in-memory token bucket per client key — process-local, matching the in-RAM demo posture.
    Not a substitute for an edge limiter, but it stops a single client from flooding the LLM path."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(max(1, capacity))
        self.refill = max(0.001, refill_per_sec)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            if len(self._buckets) > 10000:
                self._buckets.clear()  # crude bound; the demo is low-volume + auth-gated
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


class _UsageQuota:
    """Per-client demo match quota — the demo's "full functionality, limited quantity" gate.

    A client can try EVERY feature, but only on a few small batches per rolling window, so nobody
    pushes a 50-résumé pipeline through the box (and the Claude subscription). In-memory + process-
    local (resets on restart), keyed on the same best-effort client key as the rate limiter — a
    conversion taste-gate, NOT billing-grade enforcement (the key is an IP/forwarded header, trivially
    evaded; copy must say "convenience", never "enforcement").

    Re-running the SAME batch (identical job + résumés) is FREE: recently-charged batch signatures are
    remembered per client, so fixing a setting and re-scoring the same files doesn't burn a match.
    `limit <= 0` disables the gate (the default — local runs and tests stay unmetered)."""

    def __init__(self, limit: int, window_sec: float) -> None:
        self.limit = int(limit)
        self.window = max(1.0, float(window_sec))
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _entry_locked(self, key: str, now: float) -> dict:
        """Return the client's window entry, rolling it over if the window elapsed (lock held)."""
        entry = self._state.get(key)
        if entry is None or now - entry["win_start"] > self.window:
            entry = {"win_start": now, "used": 0, "charged": OrderedDict()}
            self._state[key] = entry
        return entry

    def remaining(self, key: str, now: float):
        if not self.enabled:
            return None
        with self._lock:
            return max(0, self.limit - self._entry_locked(key, now)["used"])

    def allowed(self, key: str, batch_sig: str, now: float) -> bool:
        """True if this client may run now: a free re-score of a known batch, or quota left."""
        if not self.enabled:
            return True
        with self._lock:
            if len(self._state) > 10000:
                self._state.clear()  # crude bound; the demo is low-volume + auth-gated
            entry = self._entry_locked(key, now)
            return batch_sig in entry["charged"] or entry["used"] < self.limit

    def charge(self, key: str, batch_sig: str, now: float) -> None:
        """Record a successful NEW run; a known batch_sig (a free re-score) does not increment."""
        if not self.enabled:
            return
        with self._lock:
            entry = self._entry_locked(key, now)
            if batch_sig not in entry["charged"]:
                entry["used"] += 1
            entry["charged"][batch_sig] = now
            entry["charged"].move_to_end(batch_sig)
            while len(entry["charged"]) > 64:
                entry["charged"].popitem(last=False)


def _split_ids(value: str) -> list[str]:
    import re

    return [s.strip() for s in re.split(r"[;,|]", value or "") if s.strip()]


def _ensure_in_memory_uploads() -> None:
    """Force multipart uploads to stay ENTIRELY in RAM (no temp-file spill).

    Starlette spools any upload part larger than `spool_max_size` (default 1 MB) to a temporary file
    on disk. For the privacy-critical demo we raise that threshold above the max allowed file size so
    a resume is never written to disk during parsing. (The per-part *acceptance* limit is set per
    request via `form(max_part_size=...)`.)"""
    import starlette.formparsers as _fp

    want = (demo_mod.MAX_FILE_MB + 2) * 1024 * 1024
    if _fp.MultiPartParser.spool_max_size < want:
        _fp.MultiPartParser.spool_max_size = want


def _start_sweeper() -> None:
    """Start ONE background daemon that purges expired demo sessions (idempotent)."""
    global _SWEEPER_STARTED
    if _SWEEPER_STARTED:
        return
    _SWEEPER_STARTED = True
    store = get_demo_store()

    def _loop() -> None:
        while True:
            time.sleep(60)
            try:
                store.sweep()
            except Exception as exc:  # noqa: BLE001 - a sweep error must never kill the thread
                _log.warning("demo session sweep failed: %s", exc)

    threading.Thread(target=_loop, name="rm-demo-sweeper", daemon=True).start()


def create_app():
    try:
        from fastapi import Depends, FastAPI, Form, HTTPException, Request
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import FileResponse, JSONResponse, Response
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError("FastAPI not installed. pip install -r requirements-extra.txt") from exc

    from .auth import ADMIN_COOKIE, assert_admin_password_strong, check_login, require_auth

    # Refuse to start if the admin password is a known-weak default (e.g. shipped admin/admin).
    assert_admin_password_strong()

    # App-level dependency => the gate covers the dashboard, every /api/* route, and the docs.
    app = FastAPI(title="Resume Matcher", version="0.1.0", dependencies=[Depends(require_auth)])
    state = get_state()
    demo_store = get_demo_store()
    demo_enabled = os.environ.get("RM_DEMO_ENABLED", "1").lower() in ("1", "true", "yes")
    _ensure_in_memory_uploads()
    _start_sweeper()
    demo_mod.sweep_stale_tmpdirs()  # mop up any crash-leftover file-direct temp dirs at startup

    # DoS guards for the public demo (defense in depth — the app is also admin-auth gated):
    demo_rate = _RateLimiter(
        demo_mod._int_env("RM_DEMO_RATE_BURST", 15),
        demo_mod._int_env("RM_DEMO_RATE_PER_MIN", 30) / 60.0,
    )
    demo_run_sem = threading.BoundedSemaphore(max(1, demo_mod._int_env("RM_DEMO_MAX_CONCURRENT_RUNS", 4)))
    # Demo match quota — "full functionality, limited quantity". 0 (the code default) = unlimited; the
    # public demo sets RM_DEMO_FREE_RUNS in the cohost compose. Re-running the same batch is free.
    demo_quota = _UsageQuota(
        demo_mod._int_env("RM_DEMO_FREE_RUNS", 0),
        demo_mod._int_env("RM_DEMO_QUOTA_WINDOW_MIN", 1440) * 60,
    )
    # Stricter limiter for auth endpoints — throttles password brute force + signup abuse per client.
    auth_rate = _RateLimiter(
        demo_mod._int_env("RM_AUTH_RATE_BURST", 10),
        demo_mod._int_env("RM_AUTH_RATE_PER_MIN", 10) / 60.0,
    )
    # Reject a too-large upload from its declared Content-Length BEFORE buffering the body in RAM.
    max_body_bytes = int(demo_mod.MAX_RESUMES * demo_mod.MAX_FILE_MB * 1024 * 1024 * 1.1) + 1_048_576

    def _require_demo() -> None:
        if not demo_enabled:
            raise HTTPException(403, "The real-data demo is disabled on this deployment.")

    # Accounts + saved projects (persistence tier). The SQLite store is created lazily on first use so
    # merely importing/constructing the app never touches disk (keeps the no-disk demo tests honest).
    _acct_holder: dict = {}

    def _acct() -> AccountStore:
        store = _acct_holder.get("store")
        if store is None:
            store = _acct_holder["store"] = AccountStore()
        return store

    auth_cookie = "rm_session"
    cookie_secure = env_flag("RM_COOKIE_SECURE", False)  # set True in prod (HTTPS) deployments
    if not cookie_secure and os.environ.get("RM_ADMIN_PASSWORD"):
        _log.warning("RM_COOKIE_SECURE is off while RM_ADMIN_PASSWORD is set (prod-like) — set "
                     "RM_COOKIE_SECURE=1 so session cookies are only sent over HTTPS.")

    def _current_user(request):
        return _acct().user_for_token(request.cookies.get(auth_cookie))

    @app.get("/", include_in_schema=False)
    def index():
        idx = _STATIC / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return {"message": "Resume Matcher API. Dashboard HTML not found; see /docs for the API."}

    @app.get("/demo", include_in_schema=False)
    def demo_page():
        page = _STATIC / "demo.html"
        if page.exists():
            return FileResponse(str(page))
        raise HTTPException(404, "Demo page not found.")

    @app.get("/login", include_in_schema=False)
    def login_page():
        page = _STATIC / "login.html"
        if page.exists():
            return FileResponse(str(page))
        raise HTTPException(404, "Sign-in page not found.")

    @app.get("/api/health")
    def health() -> dict:
        # Real readiness probe (auth-exempt): confirms the process booted, the dashboards it serves are
        # present, and the in-memory session store is responsive. The Docker HEALTHCHECK and the
        # auto-deploy poller gate on this; a broken deploy returns 503 and is rolled back. No PII.
        ready = (_STATIC / "index.html").exists()
        try:
            sessions = demo_store.active_count()
        except Exception:  # noqa: BLE001 - store unresponsive => not ready
            ready, sessions = False, -1
        body = {
            "status": "ok" if ready else "unhealthy",
            "version": app.version,
            "demo_enabled": demo_enabled,
            "active_sessions": sessions,
        }
        if not ready:
            raise HTTPException(503, body)
        return body

    @app.get("/api/status")
    def status() -> dict:
        return state.status()

    @app.post("/api/load-synthetic")
    def load_synthetic(regenerate: bool = False, n_students: int = 60, n_jobs: int = 12) -> dict:
        if regenerate:
            return state.regenerate_synthetic(n_students=n_students, n_jobs=n_jobs)
        return state.load_synthetic(n_students=n_students, n_jobs=n_jobs)

    @app.get("/api/jobs")
    def jobs() -> list[dict]:
        return state.jobs_overview()

    @app.get("/api/jobs/{job_id}/shortlist")
    def shortlist(job_id: str) -> dict:
        data = state.job_shortlist(job_id)
        if data is None:
            raise HTTPException(404, f"No shortlist for job {job_id!r} (load data first?)")
        return data

    @app.get("/api/candidates")
    def candidates() -> list[str]:
        return state.candidate_ids()

    @app.get("/api/candidates/{candidate_id}")
    def candidate(candidate_id: str) -> dict:
        data = state.candidate_view(candidate_id)
        if data is None:
            raise HTTPException(404, f"Unknown candidate {candidate_id!r}")
        return data

    @app.get("/api/audit")
    def audit() -> dict:
        return state.audit()

    @app.post("/api/score")
    def score(candidate: CandidateProfile, job: JobSpec) -> dict:
        # NOTE: in deployment, run redaction + consent checks before this point.
        return evaluate(candidate, job).model_dump()

    # ---- Ephemeral real-data demo: upload 1 job + up to N resumes, scored then forgotten --------
    @app.get("/api/demo/config")
    def demo_config(request: Request) -> dict:
        from ..inference.adapters import claude_cli as _cc

        return {
            "enabled": demo_enabled,
            "max_resumes": demo_mod.MAX_RESUMES,
            "max_file_mb": demo_mod.MAX_FILE_MB,
            "max_jobs": demo_mod.MAX_JOBS,
            "ttl_minutes": demo_mod.TTL_MINUTES,
            "supported_exts": sorted(SUPPORTED_EXTS),
            "backend": demo_mod.DEMO_BACKEND,
            "model": _cc.model_name(),
            "claude_available": _cc.available(),
            "active_sessions": demo_store.active_count(),
            # "full functionality, limited quantity" gate (0 = unlimited; the meter hides itself).
            "free_runs": demo_quota.limit if demo_quota.enabled else 0,
            "runs_remaining": demo_quota.remaining(_client_key(request), time.time()),
        }

    @app.get("/api/demo/skills")
    def demo_skills(q: str = "", limit: int = 10) -> list[dict]:
        from ..matching.taxonomy import search_skills

        return search_skills(q, limit=max(1, min(25, limit)))

    @app.post("/api/demo/parse-job")
    def demo_parse_job(
        job_text: str = Form(""), title: str = Form(""), employer: str = Form("")
    ) -> dict:
        _require_demo()
        spec, detected = parse_job_posting(job_text, title=title, employer=employer)
        return {
            "title": spec.title,
            "employer": spec.employer,
            "min_education": spec.min_education,
            "detected_skills": skill_options(detected),
            "required_skills": skill_options(spec.required_skills),
            "preferred_skills": skill_options(spec.preferred_skills),
        }

    @app.post("/api/demo/run")
    async def demo_run(request: Request) -> dict:
        # Parse multipart ourselves so we can (a) accept files up to RM_DEMO_MAX_FILE_MB instead of
        # Starlette's 1 MB default, and (b) cap the upload (count + per-file size) at the framework
        # edge. Combined with _ensure_in_memory_uploads(), nothing spills to disk.
        _require_demo()
        if not demo_rate.allow(_client_key(request), time.time()):
            raise HTTPException(429, "Too many requests — please slow down and retry shortly.")
        clen = request.headers.get("content-length", "")
        if clen.isdigit() and int(clen) > max_body_bytes:
            raise HTTPException(
                413,
                f"Upload too large. Limit: {demo_mod.MAX_RESUMES} files, {demo_mod.MAX_FILE_MB} MB each.",
            )
        max_part = demo_mod.MAX_FILE_MB * 1024 * 1024
        try:
            form = await request.form(
                max_files=demo_mod.MAX_RESUMES, max_fields=50, max_part_size=max_part
            )
        except Exception as exc:  # noqa: BLE001 - malformed/oversized upload -> clean 4xx
            # Log the raw cause server-side; never echo internal exception text to the client.
            _log.warning("demo upload rejected at multipart parse: %s", exc)
            raise HTTPException(
                400,
                f"Upload rejected (too large, too many files, or malformed). "
                f"Limit: {demo_mod.MAX_RESUMES} files, {demo_mod.MAX_FILE_MB} MB each.",
            )
        resume_parts = [v for v in form.getlist("resumes") if hasattr(v, "read")]
        files = [((p.filename or ""), await p.read()) for p in resume_parts]

        def field(name: str) -> str:
            v = form.get(name)
            return v if isinstance(v, str) else ""

        def num(name: str):
            try:
                return float(field(name)) if field(name).strip() else None
            except ValueError:
                return None

        # Demo match quota: full functionality, limited quantity. Build a signature of this exact batch
        # (job fields + résumé bytes) so re-scoring the SAME inputs is free, but a new batch is charged.
        # Reject over-quota with 402 + an upgrade prompt BEFORE the expensive parse/LLM work.
        ckey = _client_key(request)
        now = time.time()
        sig_parts = [field("job_text"), field("required_skills"), field("preferred_skills"),
                     field("must_have_skills"), field("min_education"), field("min_years")]
        sig_parts += sorted(hashlib.sha256(data).hexdigest() for _, data in files)
        batch_sig = hashlib.sha256("\x00".join(sig_parts).encode("utf-8")).hexdigest()
        if not demo_quota.allowed(ckey, batch_sig, now):
            raise HTTPException(402, detail={
                "upgrade": True,
                "error": "You've used all your free demo matches.",
                "message": (
                    "The demo lets you try every feature on a few small batches so you can see how it "
                    "works. To run your full pipeline — all your roles and résumés — let's get you set "
                    "up with full access."
                ),
                "limit": demo_quota.limit,
                "remaining": 0,
            })

        # Cap concurrent scoring runs (each does parsing + LLM calls) so a burst can't exhaust the
        # box; reject fast with 429 rather than queueing unbounded work.
        if not demo_run_sem.acquire(blocking=False):
            raise HTTPException(429, "The demo is busy scoring other uploads — please retry in a moment.")
        try:
            # run_demo is synchronous + CPU-bound (PDF/DOCX parse, matching); keep it off the event
            # loop so one upload doesn't stall other requests on this worker.
            sess = await run_in_threadpool(
                run_demo,
                store=demo_store,
                job_text=field("job_text"),
                title=field("title"),
                employer=field("employer"),
                required_skills=_split_ids(field("required_skills")) or None,
                preferred_skills=_split_ids(field("preferred_skills")) or None,
                must_have_skills=_split_ids(field("must_have_skills")) or None,
                min_education=field("min_education") or None,
                min_years=num("min_years"),
                files=files,
            )
        except DemoError as exc:
            raise HTTPException(400, str(exc))
        finally:
            demo_run_sem.release()
            files = []  # drop the uploaded bytes promptly
            for p in resume_parts:
                try:
                    await p.close()  # release the in-memory spool for each upload
                except Exception as exc:  # noqa: BLE001
                    _log.debug("upload part close failed: %s", exc)
        # Charge only on success (a failed run never burns a match); a known batch_sig is a free re-score.
        demo_quota.charge(ckey, batch_sig, now)
        body = sess.to_dict()
        if demo_quota.enabled:
            body["quota"] = {"limit": demo_quota.limit,
                             "remaining": demo_quota.remaining(ckey, time.time())}
        return body

    @app.get("/api/demo/session/{session_id}")
    def demo_session(session_id: str) -> dict:
        _require_demo()
        sess = demo_store.get(session_id)
        if sess is None:
            raise HTTPException(404, "Session not found — it was deleted or expired.")
        return sess.to_dict()

    @app.delete("/api/demo/session/{session_id}")
    def demo_delete(session_id: str) -> dict:
        _require_demo()
        deleted = demo_store.delete(session_id)
        return {"deleted": deleted, "message": "Your data has been deleted." if deleted else
                "Nothing to delete (already gone)."}

    # Export the (already de-identified) shortlist. Bytes are built in RAM and streamed — NOTHING is
    # written to disk, consistent with the ephemeral posture. A full functionality the demo client can
    # use on any session they scored (the only limit is the match quota, not the export).
    @app.get("/api/demo/session/{session_id}/export.csv")
    def demo_export_csv(session_id: str):
        _require_demo()
        sess = demo_store.get(session_id)
        if sess is None:
            raise HTTPException(404, "Session not found — it was deleted or expired.")
        from .serialize import shortlist_csv

        return Response(
            content=shortlist_csv(sess.to_dict()),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="shortlist.csv"'},
        )

    @app.get("/api/demo/session/{session_id}/export.json")
    def demo_export_json(session_id: str):
        _require_demo()
        sess = demo_store.get(session_id)
        if sess is None:
            raise HTTPException(404, "Session not found — it was deleted or expired.")
        return JSONResponse(
            content=sess.to_dict(),
            headers={"Content-Disposition": 'attachment; filename="shortlist.json"'},
        )

    # Multi-job fit grid: score the same résumés against several roles (job_text repeated). Same DoS
    # guards + match quota as /run (one grid = one match); each role is scored via the normal run_demo.
    @app.post("/api/demo/run-grid")
    async def demo_run_grid(request: Request) -> dict:
        _require_demo()
        if not demo_rate.allow(_client_key(request), time.time()):
            raise HTTPException(429, "Too many requests — please slow down and retry shortly.")
        clen = request.headers.get("content-length", "")
        if clen.isdigit() and int(clen) > max_body_bytes:
            raise HTTPException(413, f"Upload too large. Limit: {demo_mod.MAX_RESUMES} files, "
                                     f"{demo_mod.MAX_FILE_MB} MB each.")
        max_part = demo_mod.MAX_FILE_MB * 1024 * 1024
        try:
            form = await request.form(
                max_files=demo_mod.MAX_RESUMES, max_fields=80, max_part_size=max_part
            )
        except Exception as exc:  # noqa: BLE001 - malformed/oversized upload -> clean 4xx
            _log.warning("demo grid upload rejected at multipart parse: %s", exc)
            raise HTTPException(400, "Upload rejected (too large, too many files, or malformed). "
                                     f"Limit: {demo_mod.MAX_RESUMES} files, {demo_mod.MAX_FILE_MB} MB each.")
        resume_parts = [v for v in form.getlist("resumes") if hasattr(v, "read")]
        files = [((p.filename or ""), await p.read()) for p in resume_parts]

        def _strs(name: str) -> list[str]:
            return [v for v in form.getlist(name) if isinstance(v, str)]

        titles, employers, texts = _strs("job_title"), _strs("job_employer"), _strs("job_text")
        jobs = [
            {"title": titles[i] if i < len(titles) else "",
             "employer": employers[i] if i < len(employers) else "",
             "job_text": text}
            for i, text in enumerate(texts) if text.strip()
        ]

        ckey = _client_key(request)
        now = time.time()
        sig_parts = list(texts) + sorted(hashlib.sha256(data).hexdigest() for _, data in files)
        batch_sig = hashlib.sha256(("grid\x00" + "\x00".join(sig_parts)).encode("utf-8")).hexdigest()
        if not demo_quota.allowed(ckey, batch_sig, now):
            raise HTTPException(402, detail={
                "upgrade": True,
                "error": "You've used all your free demo matches.",
                "message": ("The demo lets you try every feature on a few small batches so you can see "
                            "how it works. To run your full pipeline — all your roles and résumés — "
                            "let's get you set up with full access."),
                "limit": demo_quota.limit, "remaining": 0,
            })

        if not demo_run_sem.acquire(blocking=False):
            raise HTTPException(429, "The demo is busy scoring other uploads — please retry in a moment.")
        try:
            sess = await run_in_threadpool(
                demo_mod.run_demo_grid, store=demo_store, jobs=jobs, files=files
            )
        except DemoError as exc:
            raise HTTPException(400, str(exc))
        finally:
            demo_run_sem.release()
            files = []
            for p in resume_parts:
                try:
                    await p.close()
                except Exception as exc:  # noqa: BLE001
                    _log.debug("upload part close failed: %s", exc)
        demo_quota.charge(ckey, batch_sig, now)
        body = sess.to_dict()
        if demo_quota.enabled:
            body["quota"] = {"limit": demo_quota.limit,
                             "remaining": demo_quota.remaining(ckey, time.time())}
        return body

    # ---- Accounts + saved projects (the "free forgets, paid remembers" persistence tier) --------
    @app.post("/api/account/register")
    async def account_register(request: Request, response: Response) -> dict:
        if not auth_rate.allow("auth:" + _client_key(request), time.time()):
            raise HTTPException(429, "Too many attempts — please wait a minute and try again.")
        data = await request.json()
        try:
            token, email = _acct().register(data.get("email", ""), data.get("password", ""))
        except AccountError as exc:
            raise HTTPException(400, str(exc))
        response.set_cookie(auth_cookie, token, max_age=cookie_max_age(),
                            httponly=True, samesite="lax", secure=cookie_secure)
        return {"email": email}

    @app.post("/api/account/login")
    async def account_login(request: Request, response: Response) -> dict:
        if not auth_rate.allow("auth:" + _client_key(request), time.time()):
            raise HTTPException(429, "Too many attempts — please wait a minute and try again.")
        data = await request.json()
        try:
            token, email = _acct().login(data.get("email", ""), data.get("password", ""))
        except AccountError as exc:
            raise HTTPException(400, str(exc))
        response.set_cookie(auth_cookie, token, max_age=cookie_max_age(),
                            httponly=True, samesite="lax", secure=cookie_secure)
        return {"email": email}

    @app.post("/api/account/logout")
    def account_logout(request: Request, response: Response) -> dict:
        _acct().logout(request.cookies.get(auth_cookie))
        response.delete_cookie(auth_cookie)
        return {"ok": True}

    @app.get("/api/account/me")
    def account_me(request: Request) -> dict:
        return {"user": _current_user(request)}

    @app.post("/api/demo/session/{session_id}/save")
    async def save_project(session_id: str, request: Request) -> dict:
        _require_demo()
        user = _current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in to save a project.")
        sess = demo_store.get(session_id)
        if sess is None:
            raise HTTPException(404, "Session not found — it was deleted or expired.")
        data = await request.json()
        payload = sess.to_dict()
        pid = _acct().save_project(user["id"], data.get("name", ""),
                                   payload.get("mode", "single"), payload)
        return {"id": pid, "name": (data.get("name") or "Untitled")}

    @app.get("/api/projects")
    def list_projects(request: Request) -> list:
        user = _current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in to view your projects.")
        return _acct().list_projects(user["id"])

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, request: Request) -> dict:
        user = _current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in to open a project.")
        proj = _acct().get_project(user["id"], pid)
        if proj is None:
            raise HTTPException(404, "Project not found.")
        return proj

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str, request: Request) -> dict:
        user = _current_user(request)
        if user is None:
            raise HTTPException(401, "Sign in first.")
        return {"deleted": _acct().delete_project(user["id"], pid)}

    # ---- Admin sign-in (form + session cookie; replaces the HTTP Basic Auth popup) --------------
    @app.post("/api/login")
    async def admin_login(request: Request, response: Response) -> dict:
        if not auth_rate.allow("auth:" + _client_key(request), time.time()):
            raise HTTPException(429, "Too many attempts — please wait a minute and try again.")
        data = await request.json()
        token = check_login(data.get("username", ""), data.get("password", ""))
        if token is None:
            raise HTTPException(401, "Wrong username or password.")
        response.set_cookie(ADMIN_COOKIE, token, max_age=cookie_max_age(),
                            httponly=True, samesite="lax", secure=cookie_secure)
        return {"ok": True}

    @app.post("/api/logout")
    def admin_logout(response: Response) -> dict:
        response.delete_cookie(ADMIN_COOKIE)
        return {"ok": True}

    # For a deployed demo, optionally pre-load the synthetic dataset so the page isn't empty.
    if os.environ.get("RM_AUTOLOAD", "").lower() in ("1", "true", "yes"):
        try:
            state.load_synthetic()
        except Exception as exc:  # noqa: BLE001 - never block startup on demo data
            _log.warning("RM_AUTOLOAD synthetic preload failed: %s", exc)

    return app


# Module-level app for `uvicorn resume_matcher.api.app:app` (only built when FastAPI is present).
try:  # pragma: no cover - optional dep
    app = create_app()
except Exception:  # noqa: BLE001
    app = None
