"""FastAPI web application: JSON API + served coordinator dashboard.

Run locally:  python scripts/serve.py            then open http://127.0.0.1:8000
Deploy:       see DEPLOY.md (Docker + Caddy auto-HTTPS behind your subdomain)

The whole app is behind an admin-password gate (auth.require_auth) when RM_ADMIN_PASSWORD is set.
The package imports fine without FastAPI installed (`app` is None until requirements-extra is in).
API is the contract; the bundled HTML dashboard is a thin, replaceable client (plan §UI).

NOTE: this module intentionally does NOT use `from __future__ import annotations` — FastAPI must
introspect real annotation objects to wire request/Form parameters, and stringized annotations can
break that. The upload route parses the multipart form manually (request.form) so it can raise the
per-file size / count limits above Starlette's defaults and keep uploads in memory.
"""
import os
import threading
import time
from pathlib import Path

from ..ingestion.job_posting import parse_job_posting, skill_options
from ..ingestion.parser import SUPPORTED_EXTS
from ..inference.schema import CandidateProfile, JobSpec
from ..matching.evaluator import evaluate
from . import demo as demo_mod
from .demo import DemoError, get_demo_store, run_demo
from .service import get_state

_STATIC = Path(__file__).with_name("static")
_SWEEPER_STARTED = False


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
            except Exception:  # noqa: BLE001 - a sweep error must never kill the thread
                pass

    threading.Thread(target=_loop, name="rm-demo-sweeper", daemon=True).start()


def create_app():
    try:
        from fastapi import Depends, FastAPI, Form, HTTPException, Request
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError("FastAPI not installed. pip install -r requirements-extra.txt") from exc

    from .auth import assert_admin_password_strong, require_auth

    # Refuse to start if the admin password is a known-weak default (e.g. shipped admin/admin).
    assert_admin_password_strong()

    # App-level dependency => the gate covers the dashboard, every /api/* route, and the docs.
    app = FastAPI(title="Resume Matcher", version="0.1.0", dependencies=[Depends(require_auth)])
    state = get_state()
    demo_store = get_demo_store()
    demo_enabled = os.environ.get("RM_DEMO_ENABLED", "1").lower() in ("1", "true", "yes")
    _ensure_in_memory_uploads()
    _start_sweeper()

    def _require_demo() -> None:
        if not demo_enabled:
            raise HTTPException(403, "The real-data demo is disabled on this deployment.")

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

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

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
    def demo_config() -> dict:
        from ..inference.adapters import claude_cli as _cc

        return {
            "enabled": demo_enabled,
            "max_resumes": demo_mod.MAX_RESUMES,
            "max_file_mb": demo_mod.MAX_FILE_MB,
            "ttl_minutes": demo_mod.TTL_MINUTES,
            "supported_exts": sorted(SUPPORTED_EXTS),
            "backend": demo_mod.DEMO_BACKEND,
            "model": _cc.model_name(),
            "claude_available": _cc.available(),
            "active_sessions": demo_store.active_count(),
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
        max_part = demo_mod.MAX_FILE_MB * 1024 * 1024
        try:
            form = await request.form(
                max_files=demo_mod.MAX_RESUMES, max_fields=50, max_part_size=max_part
            )
        except Exception as exc:  # noqa: BLE001 - malformed/oversized upload -> clean 4xx
            raise HTTPException(
                400,
                f"Upload rejected (too large, too many files, or malformed). "
                f"Limit: {demo_mod.MAX_RESUMES} files, {demo_mod.MAX_FILE_MB} MB each. [{exc}]",
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
            files = []  # drop the uploaded bytes promptly
            for p in resume_parts:
                try:
                    await p.close()  # release the in-memory spool for each upload
                except Exception:  # noqa: BLE001
                    pass
        return sess.to_dict()

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

    # For a deployed demo, optionally pre-load the synthetic dataset so the page isn't empty.
    if os.environ.get("RM_AUTOLOAD", "").lower() in ("1", "true", "yes"):
        try:
            state.load_synthetic()
        except Exception:  # noqa: BLE001 - never block startup on demo data
            pass

    return app


# Module-level app for `uvicorn resume_matcher.api.app:app` (only built when FastAPI is present).
try:  # pragma: no cover - optional dep
    app = create_app()
except Exception:  # noqa: BLE001
    app = None
