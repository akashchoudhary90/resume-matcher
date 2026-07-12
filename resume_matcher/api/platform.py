"""Platform routes (docs/PLATFORM.md) — mounted by create_app() only when RM_PLATFORM_ENABLED=1.

The Phase-1 surface: JD-autofill extraction (202 + generic job poll), postings CRUD + lifecycle,
the coordinator approval queue, org-link approvals, and the skills typeahead. Everything here
authenticates PER USER via require_role — independent of the shared admin gate (auth.py exempts
these paths when the platform flag is on, because employers/students/coordinators have their own
accounts, not the admin password).

Corrections→eval: when a posting is created from a reviewed extraction draft, the diff between
what the pipeline extracted and what the human submitted is appended to
data/eval/jd_extraction_corrections.jsonl (RM_JD_CORRECTIONS_PATH) — the measure-then-improve
loop the matching engine already runs, extended to extraction (docs/JD_AUTOFILL.md §4).

NOTE: no `from __future__ import annotations` — FastAPI introspects real annotation objects.
"""
import base64
import json
import logging
import os
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import env_int, env_str
from ..ingestion.posting_extract import PostingExtractError, extract_posting_draft
from ..ingestion.parser import ParseError
from ..matching.taxonomy import search_skills
from ..stores.platform import OrgStore, PostingError, PostingStore
from ..workers.runner import get_job_store, register_handler
from .auth import require_role

_log = logging.getLogger("resume_matcher.api.platform")

router = APIRouter()

_MAX_JD_MB = 4
_CORRECTION_FIELDS = ("title", "location", "work_mode", "employment_type", "pay_min", "pay_max",
                      "pay_currency", "pay_period", "apply_deadline", "start_date",
                      "min_education", "min_years", "application_method")
# Never record contact payloads in eval data — field names + corrections only (JD_AUTOFILL §4).
_CORRECTION_REDACTED = ("application_url", "application_email")


def _posting_store() -> PostingStore:
    return PostingStore()


def _org_store() -> OrgStore:
    return OrgStore()


# ---- tiny per-user rate limiter for the LLM-backed extract route --------------------------------
class _PerUserRate:
    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int) -> bool:
        per_min = max(1, env_int("RM_PLATFORM_EXTRACT_PER_MIN", 6))
        now = time.time()
        with self._lock:
            if len(self._hits) > 10000:
                self._hits.clear()
            hits = [t for t in self._hits.get(user_id, []) if now - t < 60]
            if len(hits) >= per_min:
                self._hits[user_id] = hits
                return False
            hits.append(now)
            self._hits[user_id] = hits
            return True


_extract_rate = _PerUserRate()


# ---- job handler: the extraction pipeline runs on the DB-backed queue ---------------------------
@register_handler("extract_posting")
def _extract_posting_job(payload: dict, progress) -> dict:
    progress(0, 1)
    file_bytes = base64.b64decode(payload["file_b64"]) if payload.get("file_b64") else None
    try:
        draft = extract_posting_draft(
            text=payload.get("text"),
            file_bytes=file_bytes,
            filename=payload.get("filename") or "",
            backend=payload.get("backend"),
            title_hint=payload.get("title") or "",
            only_role=payload.get("only_role") or "",
        )
    except (PostingExtractError, ParseError) as exc:
        raise RuntimeError(str(exc)) from exc  # client-facing message in the job's error field
    progress(1, 1)
    return {"draft": draft.model_dump(mode="json")}


# ---- generic job poll ----------------------------------------------------------------------------
def _job_visible_to(job: dict, user: dict) -> bool:
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


# ---- JD-autofill extraction (the flagship) -------------------------------------------------------
@router.post("/api/postings/extract", status_code=202)
async def start_extract(request: Request,
                        user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    """Paste text or upload a JD file (multipart field `jd`); returns 202 + a poll URL. The
    pipeline runs on the job queue so a slow LLM pass never has to survive one HTTP request."""
    if not _extract_rate.allow(user["id"]):
        raise HTTPException(429, "Too many extractions — wait a minute and try again.")
    payload: dict = {"backend": env_str("RM_PLATFORM_EXTRACT_BACKEND", "claude_cli")}
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        text = str(form.get("job_text") or "")
        payload["text"] = text if text.strip() else None
        payload["title"] = str(form.get("title") or "")
        payload["only_role"] = str(form.get("only_role") or "")
        upload = form.get("jd")
        if upload is not None and getattr(upload, "filename", ""):
            data = await upload.read()
            if len(data) > _MAX_JD_MB * 1024 * 1024:
                raise HTTPException(413, f"JD file too large (max {_MAX_JD_MB} MB).")
            payload["file_b64"] = base64.b64encode(data).decode("ascii")
            payload["filename"] = upload.filename
    else:
        body = await request.json()
        text = str(body.get("job_text") or "")
        payload["text"] = text if text.strip() else None
        payload["title"] = str(body.get("title") or "")
        payload["only_role"] = str(body.get("only_role") or "")
    if not payload.get("text") and not payload.get("file_b64"):
        raise HTTPException(400, "Paste the posting text or upload a JD file.")
    job_id = get_job_store().enqueue("extract_posting", payload, owner_user_id=user["id"])
    return {"job_id": job_id, "poll": f"/api/jobs/{job_id}"}


# ---- postings CRUD + lifecycle -------------------------------------------------------------------
def _can_view(posting: dict, user: dict) -> bool:
    if user["role"] in ("coordinator", "admin"):
        return True
    if user["role"] == "employer":
        return posting["org_id"] is not None and posting["org_id"] == user.get("org_id")
    return posting["status"] == "live"  # students see live postings only


def _require_own_org_posting(posting: dict, user: dict) -> None:
    if user["role"] in ("coordinator", "admin"):
        return
    if user["role"] != "employer" or posting["org_id"] != user.get("org_id"):
        raise HTTPException(403, "This posting belongs to another organization.")


@router.post("/api/postings", status_code=201)
def create_posting(body: dict, user: dict = Depends(require_role("employer", "coordinator",
                                                                 "admin"))):
    """Create a draft from the reviewed form. When the body carries the extraction `draft`, the
    human's corrections are appended to the eval set before the draft is stored."""
    fields = body.get("fields") or {}
    skills = body.get("skills") or []
    extraction = body.get("extraction")  # {"draft": {...}} from the review page (optional)
    try:
        posting_id = _posting_store().create(
            created_by=user["id"], org_id=user.get("org_id"), fields=fields, skills=skills,
            extraction=extraction, school_id=user.get("school_id") or 1)
    except PostingError as exc:
        raise HTTPException(400, str(exc))
    if extraction and isinstance(extraction.get("draft"), dict):
        _record_corrections(extraction["draft"], fields, skills)
    return {"posting_id": posting_id, "status": "draft"}


@router.get("/api/postings")
def list_postings(status: str = "", user: dict = Depends(require_role())):
    store = _posting_store()
    if user["role"] in ("coordinator", "admin"):
        return {"postings": store.list(status=status or None)}
    if user["role"] == "employer":
        if user.get("org_id") is None:
            return {"postings": []}
        return {"postings": store.list(status=status or None, org_id=user["org_id"])}
    return {"postings": store.list(status="live")}  # students


@router.get("/api/postings/{posting_id}")
def get_posting(posting_id: str, user: dict = Depends(require_role())):
    posting = _posting_store().get(posting_id)
    if posting is None or not _can_view(posting, user):
        raise HTTPException(404, "No such posting.")
    if user["role"] == "student":
        posting.pop("extraction", None)  # students get the posting, not the extraction internals
    posting["events"] = _posting_store().events(posting_id) \
        if user["role"] in ("coordinator", "admin") else []
    return posting


@router.patch("/api/postings/{posting_id}")
def patch_posting(posting_id: str, body: dict,
                  user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    store = _posting_store()
    posting = store.get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    if posting["status"] not in ("draft", "rejected") and user["role"] == "employer":
        raise HTTPException(409, "Only draft or rejected postings can be edited.")
    store.update_fields(posting_id, body.get("fields") or {}, body.get("skills"))
    return store.get(posting_id)


@router.post("/api/postings/{posting_id}/submit")
def submit_posting(posting_id: str,
                   user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    """draft/rejected -> pending_review. Employers need their org's school link approved first —
    the Handshake trust gate."""
    store = _posting_store()
    posting = store.get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    if user["role"] == "employer":
        if _org_store().link_status(posting["org_id"] or -1) != "approved":
            raise HTTPException(409, "Your organization hasn't been approved by career services "
                                     "yet — the posting stays a draft until it is.")
    try:
        return store.transition(posting_id, "pending_review", actor_user_id=user["id"])
    except PostingError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/postings/{posting_id}/close")
def close_posting(posting_id: str,
                  user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    store = _posting_store()
    posting = store.get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    try:
        return store.transition(posting_id, "closed", actor_user_id=user["id"])
    except PostingError as exc:
        raise HTTPException(409, str(exc))


# ---- coordinator ----------------------------------------------------------------------------------
@router.get("/api/coordinator/queue")
def coordinator_queue(user: dict = Depends(require_role("coordinator", "admin"))):
    return {
        "postings": _posting_store().list(status="pending_review"),
        "org_links": _org_store().pending_links(),
    }


@router.post("/api/coordinator/postings/{posting_id}/approve")
def approve_posting(posting_id: str, body: dict | None = None,
                    user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return _posting_store().transition(posting_id, "live", actor_user_id=user["id"],
                                           note=(body or {}).get("note", ""))
    except PostingError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/coordinator/postings/{posting_id}/reject")
def reject_posting(posting_id: str, body: dict | None = None,
                   user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return _posting_store().transition(posting_id, "rejected", actor_user_id=user["id"],
                                           note=(body or {}).get("note", ""))
    except PostingError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/coordinator/org-links/{org_id}/approve")
def approve_org_link(org_id: int, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        _org_store().set_link_status(org_id, "approved", reviewed_by=user["id"])
    except PostingError as exc:
        raise HTTPException(404, str(exc))
    return {"org_id": org_id, "status": "approved"}


@router.post("/api/coordinator/org-links/{org_id}/revoke")
def revoke_org_link(org_id: int, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        _org_store().set_link_status(org_id, "revoked", reviewed_by=user["id"])
    except PostingError as exc:
        raise HTTPException(404, str(exc))
    return {"org_id": org_id, "status": "revoked"}


# ---- skills typeahead (promoted from the demo-gated route) ---------------------------------------
@router.get("/api/skills")
def skills_typeahead(q: str = "", user: dict = Depends(require_role())):
    return {"skills": search_skills(q)}


# ---- corrections -> eval set (docs/JD_AUTOFILL.md §4) --------------------------------------------
def _corrections_path() -> str:
    return env_str("RM_JD_CORRECTIONS_PATH",
                   os.path.join("data", "eval", "jd_extraction_corrections.jsonl"))


def _draft_value(draft: dict, name: str):
    field = draft.get(name)
    if isinstance(field, dict) and "value" in field:
        return field.get("value"), field.get("method"), field.get("confidence")
    return None, None, None


_FINAL_KEYS = {  # posting-form column -> draft field name
    "title": "title", "work_mode": "work_mode", "employment_type": "employment_type",
    "apply_deadline": "application_deadline", "start_date": "start_date",
    "min_education": "min_education", "min_years": "min_years",
}


def _record_corrections(draft: dict, final_fields: dict, final_skills: list[dict]) -> None:
    """Append per-field extracted-vs-submitted records. Best-effort: eval logging must never
    block a posting."""
    try:
        sha = (draft.get("extraction_meta") or {}).get("source_sha256", "")
        model = (draft.get("extraction_meta") or {}).get("model")
        records = []
        now = time.time()
        for col, field_name in _FINAL_KEYS.items():
            extracted, method, confidence = _draft_value(draft, field_name)
            corrected = final_fields.get(col)
            if extracted is None and corrected in (None, ""):
                continue
            records.append({"posting_sha": sha, "field": field_name, "extracted": extracted,
                            "corrected": corrected, "changed": extracted != corrected,
                            "method": method, "confidence": confidence, "model": model,
                            "ts": now})
        for name in _CORRECTION_REDACTED:  # record THAT they changed, never the values
            extracted, method, confidence = _draft_value(draft, "application")
            if isinstance(extracted, dict):
                records.append({"posting_sha": sha, "field": name, "extracted": "[redacted]",
                                "corrected": "[redacted]", "changed": None, "method": method,
                                "confidence": confidence, "model": model, "ts": now})
                break
        draft_skills = {s.get("skill_id"): s.get("bucket") for s in draft.get("skills", [])}
        final = {s.get("skill_id"): s.get("bucket") for s in final_skills}
        for sid in sorted(set(draft_skills) | set(final)):
            if draft_skills.get(sid) != final.get(sid):
                records.append({"posting_sha": sha, "field": f"skills[{sid}].bucket",
                                "extracted": draft_skills.get(sid), "corrected": final.get(sid),
                                "changed": True, "method": "merged", "confidence": None,
                                "model": model, "ts": now})
        if not records:
            return
        path = _corrections_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        _log.warning("failed to record extraction corrections", exc_info=True)
