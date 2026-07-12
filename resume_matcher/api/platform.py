"""Platform routes (docs/PLATFORM.md) — mounted by create_app() only when RM_PLATFORM_ENABLED=1.

This module grows with the slices in docs/IMPLEMENTATION.md: Slice B ships the generic job-poll
route; Slice F adds postings/coordinator routes and registers the extract_posting job handler.
Everything here authenticates PER USER via require_role (independent of the shared admin gate).

NOTE: no `from __future__ import annotations` — FastAPI introspects real annotation objects.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..workers.runner import get_job_store
from .auth import require_role

router = APIRouter()


def _job_visible_to(job: dict, user: dict) -> bool:
    """Owners see their jobs; coordinators/admins see all (incl. system jobs with no owner)."""
    if user.get("role") in ("coordinator", "admin"):
        return True
    return job.get("owner_user_id") == user.get("id")


@router.get("/api/jobs/{job_id}")
def poll_job(job_id: str, user: dict = Depends(require_role())):
    """Generic 202-poll endpoint for any platform job (the demo's /session poll, DB-backed)."""
    job = get_job_store().get(job_id)
    if job is None or not _job_visible_to(job, user):
        raise HTTPException(404, "No such job.")  # 404 for unauthorized too — don't leak existence
    out = {
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": {"done": job["progress_done"], "total": job["progress_total"]},
    }
    if job["status"] == "done":
        out["result"] = job["result"]
    elif job["status"] == "error":
        out["error"] = job["error"]
    return out
