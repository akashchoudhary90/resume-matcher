"""FastAPI web application: JSON API + served coordinator dashboard.

Run locally:  python scripts/serve.py            then open http://127.0.0.1:8000
Deploy:       see DEPLOY.md (Docker + Caddy auto-HTTPS behind your subdomain)

The whole app is behind an admin-password gate (auth.require_auth) when RM_ADMIN_PASSWORD is set.
The package imports fine without FastAPI installed (`app` is None until requirements-extra is in).
API is the contract; the bundled HTML dashboard is a thin, replaceable client (plan §UI).
"""
from __future__ import annotations

import os
from pathlib import Path

from ..inference.schema import CandidateProfile, JobSpec
from ..matching.evaluator import evaluate
from .service import get_state

_STATIC = Path(__file__).with_name("static")


def create_app():
    try:
        from fastapi import Depends, FastAPI, HTTPException
        from fastapi.responses import FileResponse
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError("FastAPI not installed. pip install -r requirements-extra.txt") from exc

    from .auth import require_auth

    # App-level dependency => the gate covers the dashboard, every /api/* route, and the docs.
    app = FastAPI(title="Resume Matcher", version="0.1.0", dependencies=[Depends(require_auth)])
    state = get_state()

    @app.get("/", include_in_schema=False)
    def index():
        idx = _STATIC / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return {"message": "Resume Matcher API. Dashboard HTML not found; see /docs for the API."}

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
