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

Phase 5 (docs/PHASE5.md §3.1) enforces, in place, the promises these routes already made:
  * A1 — /api/graph/repudiate no longer deletes anything. An anonymous assertion buys a CHALLENGE
    (prove the address) or a coordinator REVIEW; both answer the same neutral 202.
  * A2/SM-M2 — a mutual without `warm_intro` is pruned from pathfinding BEFORE ranking, so the
    surface is not a consent oracle; the binding check lives inside IntroStore.create's txn.
  * A5/A6 — revoking a consent DELETES what it authorized (resume blob + scores, audit self-ID).
  * A7/FH-H3 — the `network_analytics` cohort filter applies to intro-equity AND network-coverage,
    and deliberately NOT to the self-ID report (whose basis is `self_id_audit`).
  * A8 — the two self-ID-backed reports are served from pinned snapshots, so a coordinator cannot
    difference two reads across one student joining the cohort.
  * B6/B7 — student posting search; student-initiated withdrawal (terminal, no reason collected).

NOTE: no `from __future__ import annotations` — FastAPI introspects real annotation objects.
"""
import base64
import json
import logging
import os
import threading
import time
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import notify
from ..config import env_int, env_str
from ..inference.redaction import redact_text
from ..inference.schema import CandidateProfile
from ..ingestion.posting_extract import PostingExtractError, extract_posting_draft
from ..ingestion.parser import (
    ParseError,
    infer_education_level,
    infer_years_experience,
)
from ..ingestion.job_posting import build_job_spec
from ..matching.evaluator import evaluate
from ..matching.taxonomy import normalize_skills, search_skills
from ..stores.db import connect as db_connect
from ..stores.engage import EngageError, EventStore, InterviewStore, MessageStore
from ..stores.graph import GraphError, NetworkStore
from ..stores.intros import IntroError, IntroStore, find_paths, path_origin
from ..stores.matches import MatchStore
from ..stores.notifications import NotificationStore
from ..stores.platform import OrgStore, PostingError, PostingStore
from ..stores.relationships import RelationshipError, RelationshipStore
from ..stores.students import CONSENT_PURPOSES, ApplicationStore, StudentError, StudentStore
from ..workers.runner import get_job_store, register_handler
from .auth import require_role

# Phase-4 graph purposes (subset of CONSENT_PURPOSES) exposed via the granular consent API. The
# A5/A6 storage/audit purposes deliberately do NOT route here (asserted by a test) — their revoke
# cascade lives on the students consent route.
_GRAPH_PURPOSES = ("contacts_upload", "graph_discoverable", "warm_intro", "network_analytics")


def _broker_verify_level(user: dict) -> str:
    """The vouch tier an accepted intro produces, derived from WHO the broker is.

    C4 turned this from a role->tier dict into a function: a coordinator-verified alum brokering an
    intro is a stronger attestation than a random peer, but alumni-ness is an ATTRIBUTE on the user
    (D4), never a role — so it cannot be expressed as a `user["role"]` lookup."""
    role = user.get("role")
    if role == "employer":
        return "employer_verified"
    if role in ("coordinator", "admin"):
        return "coordinator"
    if role == "student" and user.get("alumni_status") == "verified":
        return "alumni_verified"
    return "self"

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


# ---- per-client rate limiting for the PUBLIC repudiation routes (A1) -----------------------------
class _RateLimiter:
    """Token bucket per client key — a local twin of app.py's. Both it and `_client_key` are
    duplicated rather than imported from `.app` (feasibility L5): that module pulls the whole demo
    graph in, and platform.py must not depend on the demo to gate a public route."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(max(1, capacity))
        self.refill = max(0.001, refill_per_sec)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            if len(self._buckets) > 10000:
                self._buckets.clear()  # crude bound, same posture as the demo limiter
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


def _client_key(request: Request) -> str:
    """Best-effort client identity for the public limiter.

    TRUST BOUNDARY (security H3): the first X-Forwarded-For hop is CALLER-SUPPLIED unless a trusted
    front (our Caddy) rewrites it, so this limiter is a speed bump against casual flooding — never
    the anti-bombing control. That job belongs to the IP-INDEPENDENT per-email and global 24h send
    caps inside NetworkStore.create_repudiation."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_repudiate_rate = _RateLimiter(3, 5 / 3600)


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
    if user["role"] == "admin":
        return True  # the only cross-school role (ops/support)
    if user["role"] == "coordinator":
        # coordinators are seeded per school and every LIST/report is school-filtered; the by-ID
        # paths must match, or School A staff could read/act on School B postings (tenant break).
        return posting["school_id"] == (user.get("school_id") or 1)
    if user["role"] == "employer":
        return posting["org_id"] is not None and posting["org_id"] == user.get("org_id")
    return posting["status"] == "live" and posting["school_id"] == (user.get("school_id") or 1)


def _require_own_org_posting(posting: dict, user: dict) -> None:
    if user["role"] == "admin":
        return
    if user["role"] == "coordinator":
        if posting["school_id"] != (user.get("school_id") or 1):
            raise HTTPException(403, "This posting belongs to another school.")
        return
    # employer: must own the posting's org; None==None must NOT pass (org-less employer footgun).
    if (user["role"] != "employer" or posting["org_id"] is None
            or posting["org_id"] != user.get("org_id")):
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
        target_school = body.get("school_id") or user.get("school_id") or 1
        posting_id = _posting_store().create(
            created_by=user["id"], org_id=user.get("org_id"), fields=fields, skills=skills,
            extraction=extraction, school_id=int(target_school))
    except PostingError as exc:
        raise HTTPException(400, str(exc))
    if extraction and isinstance(extraction.get("draft"), dict):
        _record_corrections(extraction["draft"], fields, skills)
    return {"posting_id": posting_id, "status": "draft"}


@router.get("/api/postings")
def list_postings(status: str = "", q: str = "", employment_type: str = "", work_mode: str = "",
                  pay_min: float | None = None, deadline_after: str = "", sort: str = "newest",
                  page: int = 1, page_size: int = 20, user: dict = Depends(require_role())):
    """B6: the student branch is a filtered/sorted/paged SEARCH over live postings in their own
    school, so its response carries {postings, total, page, page_size}. Employer/coordinator
    branches are unchanged review surfaces and keep the bare {postings} shape."""
    store = _posting_store()
    school = user.get("school_id") or 1
    if user["role"] in ("coordinator", "admin"):
        return {"postings": store.list(status=status or None, school_id=school)}
    if user["role"] == "employer":
        if user.get("org_id") is None:
            return {"postings": []}
        return {"postings": store.list(status=status or None, org_id=user["org_id"],
                                       school_id=None)}  # an org can span schools
    return store.search(school_id=school, status="live", q=q, employment_type=employment_type,
                        work_mode=work_mode, pay_min=pay_min, deadline_after=deadline_after,
                        sort=sort, page=page, page_size=page_size)


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
        if _org_store().link_status(posting["org_id"] or -1,
                                    school_id=posting["school_id"]) != "approved":
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
    school = user.get("school_id") or 1
    return {
        "postings": _posting_store().list(status="pending_review", school_id=school),
        "org_links": _org_store().pending_links(school),
    }


@router.post("/api/coordinator/postings/{posting_id}/approve")
def approve_posting(posting_id: str, body: dict | None = None,
                    user: dict = Depends(require_role("coordinator", "admin"))):
    existing = _posting_store().get(posting_id)
    if existing is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(existing, user)  # a coordinator may only review their own school
    try:
        posting = _posting_store().transition(posting_id, "live", actor_user_id=user["id"],
                                              note=(body or {}).get("note", ""))
    except PostingError as exc:
        raise HTTPException(409, str(exc))
    # event-driven matching: a posting going live is THE moment its shortlist gets computed
    get_job_store().enqueue("match_posting", {"posting_id": posting_id})
    _notify_creator(posting, "Your posting is live",
                    f"“{posting['title']}” was approved by career services and is now live.",
                    kind="posting_approved")
    return posting


@router.post("/api/coordinator/postings/{posting_id}/reject")
def reject_posting(posting_id: str, body: dict | None = None,
                   user: dict = Depends(require_role("coordinator", "admin"))):
    note = (body or {}).get("note", "")
    existing = _posting_store().get(posting_id)
    if existing is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(existing, user)  # a coordinator may only review their own school
    try:
        posting = _posting_store().transition(posting_id, "rejected", actor_user_id=user["id"],
                                              note=note)
    except PostingError as exc:
        raise HTTPException(409, str(exc))
    # the reviewer's note is coordinator free text and stays OUT of the composed body — the
    # notifications table holds server-composed text only; the note itself rides the posting's
    # event log, which is where the employer reads it anyway.
    _notify_creator(posting, "Your posting needs changes",
                    f"“{posting['title']}” was returned by career services"
                    + (" with a reviewer's note — open the posting to read it." if note else "."),
                    kind="posting_rejected")
    return posting


@router.post("/api/coordinator/org-links/{org_id}/approve")
def approve_org_link(org_id: int, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        _org_store().set_link_status(org_id, "approved", reviewed_by=user["id"],
                                     school_id=user.get("school_id") or 1)
    except PostingError as exc:
        raise HTTPException(404, str(exc))
    return {"org_id": org_id, "status": "approved"}


@router.post("/api/coordinator/org-links/{org_id}/revoke")
def revoke_org_link(org_id: int, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        _org_store().set_link_status(org_id, "revoked", reviewed_by=user["id"],
                                     school_id=user.get("school_id") or 1)
    except PostingError as exc:
        raise HTTPException(404, str(exc))
    return {"org_id": org_id, "status": "revoked"}


# ---- schools (Slice V: the multi-school marketplace) ----------------------------------------------
@router.get("/api/schools")
def list_schools():
    """Public: the register form needs the school list before sign-in."""
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT id, name FROM schools ORDER BY name").fetchall()
    return {"schools": [dict(r) for r in rows]}


@router.post("/api/schools", status_code=201)
def create_school(body: dict, user: dict = Depends(require_role("admin"))):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "A school needs a name.")
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT id FROM schools WHERE name=?", (name,)).fetchone()
        if row:
            return {"school_id": row["id"], "name": name}
        cur = conn.execute("INSERT INTO schools(name, created_at) VALUES(?,?)",
                           (name, time.time()))
        conn.commit()
        return {"school_id": cur.lastrowid, "name": name}


@router.post("/api/orgs/me/school-links", status_code=201)
def request_school_link(body: dict, user: dict = Depends(require_role("employer"))):
    """An employer asks to recruit at ANOTHER school; that school's coordinator approves."""
    if user.get("org_id") is None:
        raise HTTPException(409, "Your account has no organization.")
    school_id = int(body.get("school_id") or 0)
    with closing(db_connect()) as conn:
        if not conn.execute("SELECT 1 FROM schools WHERE id=?", (school_id,)).fetchone():
            raise HTTPException(400, "Unknown school.")
        conn.execute(
            "INSERT OR IGNORE INTO employer_school_links(org_id, school_id, created_at) "
            "VALUES(?,?,?)",
            (user["org_id"], school_id, time.time()),
        )
        conn.commit()
    return {"org_id": user["org_id"], "school_id": school_id,
            "status": OrgStore().link_status(user["org_id"], school_id)}


# ---- skills typeahead (promoted from the demo-gated route) ---------------------------------------
@router.get("/api/skills")
def skills_typeahead(q: str = "", user: dict = Depends(require_role())):
    return {"skills": search_skills(q)}


# ---- students: profile, consents, resume (Slice I) ------------------------------------------------
def _student_store() -> StudentStore:
    return StudentStore()


@router.get("/api/students/me/profile")
def my_profile(user: dict = Depends(require_role("student"))):
    store = _student_store()
    return {"profile": store.get_profile(user["id"]), "consents": store.consents(user["id"]),
            "resume": store.resume_meta(user["id"])}


@router.put("/api/students/me/profile")
def update_profile(body: dict, user: dict = Depends(require_role("student"))):
    grad = body.get("grad_year")
    return {"profile": _student_store().upsert_profile(
        user["id"], program=str(body.get("program") or ""),
        grad_year=int(grad) if isinstance(grad, (int, float)) and 1990 < int(grad) < 2100 else None,
        work_auth_simple=str(body.get("work_auth_simple") or ""),
        visibility=bool(body.get("visibility", True)),
        school_id=user.get("school_id") or 1)}


def _revoke_cascade(user_id: int, purpose: str) -> None:
    """A5/A6: a revoke must DELETE what the consent authorized, not merely stop new writes — a
    toggle that leaves the data behind is a broken promise, not a consent.

    `graph_discoverable` keeps its own cascade in set_graph_consent (edges + discovery tokens);
    those purposes never reach this route (asserted by a test)."""
    if purpose == "profile_matching":
        MatchStore().delete_for_student(user_id)      # already-computed scores go too
    elif purpose == "resume_storage":
        _student_store().delete_resume(user_id)       # blob + redacted text, hard
        MatchStore().delete_for_student(user_id)      # every score was derived from that resume
    elif purpose == "self_id_audit":
        from ..stores.audit_store import AuditDB      # deferred: the audit plane is a separate file

        AuditDB().delete_self_id(_candidate_ref(user_id))


@router.post("/api/students/me/consents")
def set_consent(body: dict, user: dict = Depends(require_role("student"))):
    purpose, granted = str(body.get("purpose") or ""), bool(body.get("granted"))
    store = _student_store()
    try:
        store.set_consent(user["id"], purpose, granted)
    except StudentError as exc:
        raise HTTPException(400, str(exc))
    if not granted:
        _revoke_cascade(user["id"], purpose)
    return {"consents": store.consents(user["id"])}


@router.get("/api/students/me/consents")
def get_consents(user: dict = Depends(require_role("student"))):
    return {"consents": _student_store().consents(user["id"]),
            "purposes": list(CONSENT_PURPOSES)}


@router.post("/api/students/me/resume", status_code=201)
async def upload_resume(request: Request, user: dict = Depends(require_role("student"))):
    form = await request.form()
    upload = form.get("resume")
    if upload is None or not getattr(upload, "filename", ""):
        raise HTTPException(400, "Attach a resume file (field name: resume).")
    data = await upload.read()
    if len(data) > _MAX_JD_MB * 1024 * 1024:
        raise HTTPException(413, f"Resume too large (max {_MAX_JD_MB} MB).")
    try:
        meta = _student_store().save_resume(user["id"], upload.filename,
                                            upload.content_type or "", data,
                                            school_id=user.get("school_id") or 1)
    except StudentError as exc:
        raise HTTPException(409, str(exc))
    except ParseError as exc:
        raise HTTPException(400, str(exc))
    # event-driven rematch: score this student against every live posting
    get_job_store().enqueue("rematch_student", {"student_id": user["id"]},
                            owner_user_id=user["id"])
    return meta


@router.delete("/api/students/me/resume")
def delete_resume(user: dict = Depends(require_role("student"))):
    removed = _student_store().delete_resume(user["id"])
    MatchStore().delete_for_student(user["id"])  # hard delete includes computed scores
    return {"deleted": removed}


# ---- applications (Slice J) ------------------------------------------------------------------------
@router.post("/api/postings/{posting_id}/apply", status_code=201)
def apply_to_posting(posting_id: str, body: dict | None = None,
                     user: dict = Depends(require_role("student"))):
    posting = _posting_store().get(posting_id)
    if posting is None or posting["status"] != "live" or not _can_view(posting, user):
        raise HTTPException(404, "No such posting.")  # incl. other schools' postings
    resume = _student_store().resume_meta(user["id"])
    if resume is None:
        raise HTTPException(409, "Upload a resume before applying.")
    try:
        app_id = ApplicationStore().apply(posting_id, user["id"], resume["id"])
    except StudentError as exc:
        raise HTTPException(409, str(exc))
    _notify_creator(posting, "New application on your posting",
                    f"A student applied to “{posting['title']}”.")
    return {"application_id": app_id, "status": "applied"}


@router.get("/api/students/me/applications")
def my_applications(user: dict = Depends(require_role("student"))):
    return {"applications": ApplicationStore().for_student(user["id"])}


@router.get("/api/postings/{posting_id}/applications")
def posting_applications(posting_id: str,
                         user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    posting = _posting_store().get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    return {"applications": ApplicationStore().for_posting(posting_id)}


@router.patch("/api/applications/{application_id}")
def update_application(application_id: str, body: dict,
                       user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    apps = ApplicationStore()
    app_row = apps.get(application_id)
    if app_row is None:
        raise HTTPException(404, "No such application.")
    posting = _posting_store().get(app_row["posting_id"])
    _require_own_org_posting(posting, user)
    to_status = str(body.get("status") or "")
    if to_status == "withdrawn":
        # B7: withdrawal is the student's speech about their own candidacy — an employer marking
        # someone "withdrawn" would put words in their mouth (and dodge the funnel's reject count).
        raise HTTPException(409, "Students withdraw their own applications.")
    try:
        updated = apps.set_status(application_id, to_status)
    except StudentError as exc:
        raise HTTPException(409, str(exc))
    _notify_user(app_row["student_id"], posting.get("school_id") or 1, "application_status",
                 "Your application status changed",
                 f"Your application to “{posting.get('title') or 'a posting'}” is now {to_status}.",
                 entity="application", entity_id=application_id)
    return updated


@router.post("/api/applications/{application_id}/withdraw")
def withdraw_application(application_id: str, user: dict = Depends(require_role("student"))):
    """B7. Student-owned, terminal, and NO reason is ever collected — a withdrawal is a fact, not a
    confession, and a free-text 'why did you leave' box is exactly the kind of thing that ends up
    read as a signal about the person."""
    apps = ApplicationStore()
    app_row = apps.get(application_id)
    if app_row is None or app_row["student_id"] != user["id"]:
        raise HTTPException(404, "No such application.")   # IDOR: same shape as a missing row
    try:
        apps.withdraw(application_id, user["id"])
    except StudentError as exc:
        raise HTTPException(409, str(exc))
    posting = _posting_store().get(app_row["posting_id"]) or {}
    _notify_creator(posting, "An applicant withdrew",
                    f"An applicant withdrew from “{posting.get('title') or 'your posting'}”.",
                    kind="application_status")
    return {"status": "withdrawn"}


@router.post("/api/applications/{application_id}/request-human-review")
def request_human_review(application_id: str, user: dict = Depends(require_role("student"))):
    try:
        ApplicationStore().request_human_review(application_id, user["id"])
    except StudentError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


# ---- the matching loop (Slice K) -------------------------------------------------------------------
def _candidate_from_row(row: dict) -> CandidateProfile:
    """CandidateProfile from stored REDACTED text — recomputed deterministically, so nothing but
    the redacted text ever reaches an adapter (boundary #3)."""
    text = row["redacted_text"] or ""
    return CandidateProfile(
        candidate_id=f"u{row['user_id']}",
        skills=normalize_skills(text),
        education_level=infer_education_level(text),
        years_experience=infer_years_experience(text),
        text=text,
    )


def _job_spec_from_posting(posting: dict):
    buckets: dict[str, list[str]] = {"must_have": [], "required": [], "preferred": []}
    for s in posting.get("skills", []):
        buckets.setdefault(s["bucket"], []).append(s["skill_id"])
    return build_job_spec(
        job_id=posting["id"], title=posting["title"], employer=posting.get("org_name") or "",
        description=posting.get("description") or "",
        required_skills=buckets["required"], preferred_skills=buckets["preferred"],
        must_have_skills=buckets["must_have"],
        min_education=posting.get("min_education"), min_years=posting.get("min_years"),
    )


@register_handler("match_posting")
def _match_posting_job(payload: dict, progress) -> dict:
    posting = PostingStore().get(payload["posting_id"])
    if posting is None or posting["status"] != "live":
        return {"scored": 0, "skipped": "posting not live"}
    spec = _job_spec_from_posting(posting)
    students = StudentStore().matchable_students(school_id=posting["school_id"])
    matches = MatchStore()
    progress(0, len(students))
    for i, row in enumerate(students):
        matches.upsert(posting["id"], row["user_id"], evaluate(_candidate_from_row(row), spec))
        progress(i + 1)
    return {"scored": len(students)}


@register_handler("rematch_student")
def _rematch_student_job(payload: dict, progress) -> dict:
    student_id = payload["student_id"]
    store = StudentStore()
    profile = store.get_profile(student_id) or {}
    school = profile.get("school_id") or 1
    rows = [r for r in store.matchable_students(school_id=school)
            if r["user_id"] == student_id]
    if not rows:
        return {"scored": 0, "skipped": "student not in match pool"}
    candidate = _candidate_from_row(rows[0])
    postings = PostingStore().list(status="live", school_id=school)
    matches, posting_store = MatchStore(), PostingStore()
    progress(0, len(postings))
    for i, summary in enumerate(postings):
        posting = posting_store.get(summary["id"])
        matches.upsert(posting["id"], student_id,
                       evaluate(candidate, _job_spec_from_posting(posting)))
        progress(i + 1)
    return {"scored": len(postings)}


def _record_exposure(viewer: dict, posting_id: str) -> None:
    """Append-only exposure event the FIRST time this human sees this posting's ranking — the
    AEDT-relevant moment (docs/PLATFORM.md graft #7)."""
    with closing(db_connect()) as conn:
        seen = conn.execute(
            "SELECT 1 FROM events WHERE actor_user_id=? AND action='shortlist_exposed' "
            "AND entity='posting' AND entity_id=?",
            (viewer["id"], posting_id),
        ).fetchone()
        if not seen:
            conn.execute(
                "INSERT INTO events(actor_user_id, action, entity, entity_id, at) "
                "VALUES(?,?,?,?,?)",
                (viewer["id"], "shortlist_exposed", "posting", posting_id, time.time()),
            )
            conn.commit()


@router.get("/api/postings/{posting_id}/shortlist")
def posting_shortlist(posting_id: str,
                      user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    posting = _posting_store().get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    _record_exposure(user, posting_id)
    return {"posting_id": posting_id, "title": posting["title"],
            "score_kind": "fit_readiness_not_hire_probability",
            "shortlist": MatchStore().shortlist(posting_id)}


@router.get("/api/students/me/matches")
def my_matches(user: dict = Depends(require_role("student"))):
    return {"score_kind": "fit_readiness_not_hire_probability",
            "matches": MatchStore().roles_for(user["id"])}


# ---- events & career fairs (Slice R) ---------------------------------------------------------------
@router.post("/api/events", status_code=201)
def create_event(body: dict, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        event_id = EventStore().create(
            created_by=user["id"], school_id=user.get("school_id") or 1,
            title=str(body.get("title") or ""), kind=str(body.get("kind") or "fair"),
            description=str(body.get("description") or ""),
            location=str(body.get("location") or ""),
            starts_at=float(body.get("starts_at") or 0),
            ends_at=float(body["ends_at"]) if body.get("ends_at") else None)
    except EngageError as exc:
        raise HTTPException(400, str(exc))
    return {"event_id": event_id, "status": "draft"}


@router.patch("/api/events/{event_id}")
def update_event(event_id: str, body: dict,
                 user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return EventStore().set_status(event_id, str(body.get("status") or ""))
    except EngageError as exc:
        raise HTTPException(409, str(exc))


@router.get("/api/events")
def list_events(user: dict = Depends(require_role())):
    store = EventStore()
    include_drafts = user["role"] in ("coordinator", "admin")
    events = store.list(school_id=user.get("school_id") or 1, include_drafts=include_drafts)
    mine = store.my_registrations(user["id"])
    for e in events:
        e["registered"] = e["id"] in mine
    return {"events": events}


@router.post("/api/events/{event_id}/register")
def register_event(event_id: str, user: dict = Depends(require_role("student", "employer"))):
    try:
        EventStore().register(event_id, user["id"], user["role"])
    except EngageError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@router.post("/api/events/{event_id}/unregister")
def unregister_event(event_id: str, user: dict = Depends(require_role("student", "employer"))):
    EventStore().unregister(event_id, user["id"])
    return {"ok": True}


@router.get("/api/events/{event_id}/attendees")
def event_attendees(event_id: str, user: dict = Depends(require_role("coordinator", "admin"))):
    return {"attendees": EventStore().attendees(event_id)}


# ---- application-thread access (shared by messaging + interviews) -----------------------------------
def _application_access(application_id: str, user: dict) -> tuple[dict, dict]:
    """The applicant, the posting org's employers, and coordinators. Everyone else: 404."""
    app_row = ApplicationStore().get(application_id)
    if app_row is None:
        raise HTTPException(404, "No such application.")
    posting = _posting_store().get(app_row["posting_id"]) or {}
    role = user["role"]
    if role in ("coordinator", "admin"):
        return app_row, posting
    if role == "student" and app_row["student_id"] == user["id"]:
        return app_row, posting
    if role == "employer" and posting.get("org_id") == user.get("org_id"):
        return app_row, posting
    raise HTTPException(404, "No such application.")


# ---- messaging (Slice S) ----------------------------------------------------------------------------
@router.get("/api/applications/{application_id}/messages")
def get_messages(application_id: str, user: dict = Depends(require_role())):
    _application_access(application_id, user)
    return {"messages": MessageStore().thread(application_id, user["id"])}


@router.post("/api/applications/{application_id}/messages", status_code=201)
def send_message(application_id: str, body: dict, user: dict = Depends(require_role())):
    app_row, posting = _application_access(application_id, user)
    try:
        sent = MessageStore().send(application_id, user["id"], str(body.get("body") or ""))
    except EngageError as exc:
        raise HTTPException(400, str(exc))
    # the thread's two sides: the applicant and the posting's creator. The message TEXT never
    # enters the notification — only the fact that one arrived (B4-lite: no user free text).
    for uid in {app_row["student_id"], posting.get("created_by")} - {user["id"]}:
        _notify_user(uid, posting.get("school_id") or 1, "message", "New message",
                     f"There's a new message on the thread for “{posting.get('title') or 'a posting'}”.",
                     entity="application", entity_id=application_id)
    return sent


@router.get("/api/messages/unread-count")
def unread_count(user: dict = Depends(require_role())):
    apps = ApplicationStore()
    if user["role"] == "student":
        ids = [a["id"] for a in apps.for_student(user["id"])]
    elif user["role"] == "employer" and user.get("org_id") is not None:
        ids = []
        for summary in _posting_store().list(org_id=user["org_id"]):
            ids += [a["id"] for a in apps.for_posting(summary["id"])]
    else:
        ids = []
    return {"unread": MessageStore().unread_count(user["id"], ids)}


# ---- interview scheduling (Slice T) -----------------------------------------------------------------
@router.post("/api/applications/{application_id}/interview-slots", status_code=201)
def propose_slots(application_id: str, body: dict,
                  user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    app_row, posting = _application_access(application_id, user)
    try:
        slots = InterviewStore().propose(application_id, user["id"], body.get("slots") or [])
    except EngageError as exc:
        raise HTTPException(400, str(exc))
    _notify_user(app_row["student_id"], posting.get("school_id") or 1, "interview_proposed",
                 "Interview times proposed",
                 f"An employer proposed interview times for “{posting.get('title') or 'a posting'}”.",
                 entity="application", entity_id=application_id)
    return {"slots": slots}


@router.get("/api/applications/{application_id}/interview-slots")
def list_slots(application_id: str, user: dict = Depends(require_role())):
    _application_access(application_id, user)
    return {"slots": InterviewStore().for_application(application_id)}


@router.post("/api/interview-slots/{slot_id}/accept")
def accept_slot(slot_id: str, user: dict = Depends(require_role("student"))):
    store = InterviewStore()
    slot = store.get(slot_id)
    if slot is None:
        raise HTTPException(404, "No such slot.")
    app_row, _ = _application_access(slot["application_id"], user)  # applicant-only via role gate
    try:
        return store.accept(slot_id)
    except EngageError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/interview-slots/{slot_id}/cancel")
def cancel_slot(slot_id: str, user: dict = Depends(require_role())):
    store = InterviewStore()
    slot = store.get(slot_id)
    if slot is None:
        raise HTTPException(404, "No such slot.")
    app_row, posting = _application_access(slot["application_id"], user)
    try:
        out = store.cancel(slot_id)
    except EngageError as exc:
        raise HTTPException(409, str(exc))
    if user["id"] != app_row["student_id"]:   # a student cancelling doesn't notify themselves
        _notify_user(app_row["student_id"], posting.get("school_id") or 1, "interview_cancelled",
                     "An interview slot was cancelled",
                     f"A proposed interview time for “{posting.get('title') or 'a posting'}” "
                     "was cancelled.",
                     entity="application", entity_id=slot["application_id"])
    return out


@router.get("/api/students/me/interviews")
def my_interviews(user: dict = Depends(require_role("student"))):
    return {"interviews": InterviewStore().upcoming_for_student(user["id"])}


# ---- voluntary self-ID + EEO/funnel reports (Slice W) -----------------------------------------------
def _candidate_ref(user_id: int) -> str:
    return f"student-{user_id}"


@router.post("/api/students/me/self-id")
def set_self_id(body: dict, user: dict = Depends(require_role("student"))):
    """Voluntary self-ID for the AGGREGATE bias audit only. Requires the self_id_audit consent;
    writes ONLY to the separate audit database (boundary #2 — never the scoring plane)."""
    from ..stores.audit_store import AuditDB

    if not _student_store().has_consent(user["id"], "self_id_audit"):
        raise HTTPException(409, "Grant the self-ID audit consent first.")
    try:
        stored = AuditDB().set_self_id(_candidate_ref(user["id"]), body.get("attrs") or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"stored": stored}


@router.delete("/api/students/me/self-id")
def delete_self_id(user: dict = Depends(require_role("student"))):
    from ..stores.audit_store import AuditDB

    return {"deleted": AuditDB().delete_self_id(_candidate_ref(user["id"]))}


def _funnel_rows(school_id: int) -> list[dict]:
    """Per-posting selection funnel over REAL applications (+ exposure + match counts).

    B7: a withdrawn application left the funnel by the STUDENT's choice, so it is not an employer
    selection decision — counting it in `applied` would depress every selection_rate below it and
    read as employer behaviour. It gets its own column instead of vanishing."""
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, o.name AS org_name, p.status, "
            "(SELECT COUNT(*) FROM match_results m WHERE m.posting_id = p.id) AS candidates_scored,"
            "(SELECT COUNT(*) FROM applications a WHERE a.posting_id = p.id "
            " AND a.status != 'withdrawn') AS applied, "
            "(SELECT COUNT(*) FROM applications a WHERE a.posting_id = p.id "
            " AND a.status = 'withdrawn') AS withdrawn, "
            "(SELECT COUNT(*) FROM applications a WHERE a.posting_id = p.id "
            " AND a.status IN ('shortlisted','advanced','hired')) AS shortlisted_or_beyond, "
            "(SELECT COUNT(*) FROM applications a WHERE a.posting_id = p.id "
            " AND a.status = 'hired') AS hired, "
            "(SELECT COUNT(*) FROM applications a WHERE a.posting_id = p.id "
            " AND a.human_review_requested = 1) AS human_review_requests, "
            "(SELECT COUNT(*) FROM events e WHERE e.action='shortlist_exposed' "
            " AND e.entity_id = p.id) AS shortlist_viewers "
            "FROM postings p LEFT JOIN orgs o ON o.id = p.org_id "
            "WHERE p.school_id=? AND p.status IN ('live','closed') ORDER BY p.created_at",
            (school_id,),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["selection_rate"] = round(row["shortlisted_or_beyond"] / row["applied"], 3) \
            if row["applied"] else None
        out.append(row)
    return out


@router.get("/api/coordinator/reports/funnel")
def funnel_report(format: str = "json",
                  user: dict = Depends(require_role("coordinator", "admin"))):
    rows = _funnel_rows(user.get("school_id") or 1)
    if format == "csv":
        import csv
        import io

        from fastapi.responses import PlainTextResponse

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["posting_id", "title", "employer", "status", "candidates_scored",
                         "applied", "withdrawn", "shortlisted_or_beyond", "hired",
                         "human_review_requests", "shortlist_viewers", "selection_rate"])
        for r in rows:
            writer.writerow([r["id"], r["title"], r["org_name"], r["status"],
                             r["candidates_scored"], r["applied"], r["withdrawn"],
                             r["shortlisted_or_beyond"], r["hired"], r["human_review_requests"],
                             r["shortlist_viewers"], r["selection_rate"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return {"score_kind": "fit_readiness_not_hire_probability", "postings": rows}


def _serve_snapshot(report_key: str, school_id: int, refs_count: int, compute) -> dict:
    """A8 serving policy for the self-ID-backed reports.

    Cell suppression stops a single read from naming anyone; it does nothing about DIFFERENCING —
    read the report, wait for one student to join the cohort, read it again, and the delta is that
    student. So a pinned snapshot is served until BOTH: it has aged past RM_AUDIT_SNAPSHOT_HOURS,
    AND the cohort has moved by at least MIN_CELL. Either condition alone still leaves a usable
    channel (time alone lets you wait out the pin; size alone lets you trigger a recompute).

    The pin lives in the AUDIT plane next to the data it summarizes — never in platform.db."""
    from ..stores.audit_store import MIN_CELL, AuditDB

    audit = AuditDB()
    snap = audit.get_snapshot(report_key, school_id)
    if snap is not None:
        aged = (time.time() - snap["computed_at"]) >= env_int("RM_AUDIT_SNAPSHOT_HOURS", 24) * 3600
        moved = abs(refs_count - snap["refs_count"]) >= MIN_CELL
        if not (aged and moved):
            return snap["payload"]
    payload = compute()
    audit.save_snapshot(report_key, school_id, payload, refs_count)
    return payload


@router.get("/api/coordinator/reports/self-id")
def self_id_report(user: dict = Depends(require_role("coordinator", "admin"))):
    """Aggregate self-ID distribution among this school's APPLICANTS, min-cell suppressed.
    The scoring plane supplies only an opaque ref list; the audit DB answers with counts —
    the aligned-egress shape from stores/data_planes.py, made persistent.

    FH-H3: the `network_analytics` cohort filter (A7) is deliberately NOT applied here. This
    report's consent basis is `self_id_audit` — the consent the respondents actually gave for
    exactly this aggregate. Layering the analytics consent on top would empty the report for every
    student who consented to the bias audit and nothing else, which is the opposite of the fix."""
    from ..stores.audit_store import AuditDB
    from ..stores.data_planes import AUDITABLE_ATTRIBUTES

    school = user.get("school_id") or 1
    with closing(db_connect()) as conn:
        refs = ["student-" + str(r["student_id"]) for r in conn.execute(
            "SELECT DISTINCT a.student_id FROM applications a "
            "JOIN postings p ON p.id = a.posting_id WHERE p.school_id=?", (school,))]

    def _compute() -> dict:
        audit = AuditDB()
        return {"applicants": len(refs),
                "attributes": {attr: audit.aggregate(refs, attr)
                               for attr in sorted(AUDITABLE_ATTRIBUTES)}}

    return _serve_snapshot("self_id", school, len(refs), _compute)


# ---- notifications (Slice M + Phase-5 B4-lite; best-effort by contract) ----------------------------
def _user_email(user_id: int) -> str | None:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    return row["email"] if row else None


def _notify_user(user_id: int | None, school_id: int, kind: str, title: str, body: str = "",
                 entity: str | None = None, entity_id: str | None = None,
                 *, email: bool = False) -> None:
    """B4-lite fan-out. Best-effort by contract, like every notification here: a failed row or a
    dead mail server must never fail the action that triggered it.

    Titles/bodies are SERVER-COMPOSED from org/posting content only — never a user's message text
    and never an email address (NotificationStore rejects the latter at the chokepoint, so account
    erasure can't leave the erased person's address inside someone else's feed). A falsy user_id
    (None, or the created_by=0 erasure sentinel) is nobody: no-op."""
    if not user_id:
        return
    try:
        NotificationStore().notify(user_id, school_id, kind, title, body, entity=entity,
                                   entity_id=entity_id,
                                   email_to=_user_email(user_id) if email else None)
    except Exception:  # noqa: BLE001
        _log.warning("notification failed", exc_info=True)


def _notify_creator(posting: dict, subject: str, body: str, kind: str | None = None) -> None:
    """The posting creator's ping. With `kind` it is a notification row + email (B4-lite); without,
    it stays email-only — the apply ping predates notifications and has no kind of its own."""
    creator = posting.get("created_by")
    if not creator:
        return  # 0 = erased-user sentinel (§6 step 26): there is nobody to notify
    try:
        if kind:
            _notify_user(creator, posting.get("school_id") or 1, kind, subject, body,
                         entity="posting", entity_id=posting.get("id"), email=True)
            return
        to = _user_email(creator)
        if to:
            notify.send(to, subject, body)
    except Exception:  # noqa: BLE001
        _log.warning("notification failed", exc_info=True)


def _school_coordinators(school_id: int) -> list[int]:
    with closing(db_connect()) as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM users WHERE school_id=? AND role='coordinator'", (school_id,))]


# ==================================================================================================
# Phase 4 — relationship graph & warm intros (docs/RELATIONSHIPS.md)
# ==================================================================================================
def _network_store() -> NetworkStore:
    return NetworkStore()


def _rel_store() -> RelationshipStore:
    return RelationshipStore()


def _intro_store() -> IntroStore:
    return IntroStore()


# ---- graph job handlers ---------------------------------------------------------------------------
@register_handler("build_edges")
def _build_edges_job(payload: dict, progress) -> dict:
    school_id = int(payload["school_id"])
    rel = _rel_store()
    made = rel.build_native_edges(school_id)
    promoted = rel.promote_shareable(school_id)
    return {"native_edges": made, "promoted": promoted}


@register_handler("graph_retention")
def _graph_retention_job(payload: dict, progress) -> dict:
    from ..stores.retention import run_retention
    return run_retention()


@register_handler("resolve_network")
def _resolve_network_job(payload: dict, progress) -> dict:
    import base64
    raw = base64.b64decode(payload["csv_b64"])
    try:
        _network_store().import_csv(int(payload["user_id"]), int(payload["school_id"]), raw)
    except GraphError as exc:
        raise RuntimeError(str(exc)) from exc
    _rel_store().promote_shareable(int(payload["school_id"]))
    return {"ok": True}   # no per-contact counts surfaced (membership-oracle fix)


# ---- Slice Z: granular consent + discovery identity + data-subject requests -----------------------
@router.get("/api/graph/consents")
def graph_consents(user: dict = Depends(require_role())):
    store = _student_store()
    return {"consents": {p: store.has_consent(user["id"], p) for p in _GRAPH_PURPOSES}}


@router.post("/api/graph/consents")
def set_graph_consent(body: dict, user: dict = Depends(require_role())):
    purpose, granted = str(body.get("purpose") or ""), bool(body.get("granted"))
    if purpose not in _GRAPH_PURPOSES:
        raise HTTPException(400, "Unknown graph consent purpose.")
    store = _student_store()
    try:
        store.set_consent(user["id"], purpose, granted)
    except StudentError as exc:
        raise HTTPException(400, str(exc))
    if purpose == "graph_discoverable":
        if granted:
            get_job_store().enqueue("build_edges", {"school_id": user.get("school_id") or 1},
                                    dedupe_key=f"build_edges:{user.get('school_id') or 1}")
        else:
            # revoking discovery immediately removes the member from every read path + edges
            _rel_store().revoke_edges_for(user["id"])
            _network_store().clear_identity(user["id"])
    return {"consents": {p: store.has_consent(user["id"], p) for p in _GRAPH_PURPOSES}}


@router.post("/api/graph/discover")
def register_discovery(body: dict, user: dict = Depends(require_role())):
    """Register the name+company a member's connections would know them by (tokens only, never
    stored as cleartext) so others' uploaded contacts can resolve to them."""
    if not _student_store().has_consent(user["id"], "graph_discoverable"):
        raise HTTPException(409, "Grant the 'discoverable' consent first.")
    n = _network_store().register_identity(
        user["id"], user.get("school_id") or 1,
        first=str(body.get("first") or ""), last=str(body.get("last") or ""),
        company=str(body.get("company") or ""), email=str(body.get("email") or ""))
    _rel_store().promote_shareable(user.get("school_id") or 1)
    return {"tokens_registered": n}


@router.delete("/api/network")
def delete_my_network(user: dict = Depends(require_role())):
    return _network_store().delete_my_network(user["id"])


def _send_repudiation_challenge(email: str, request_id: str, token: str) -> None:
    """Best-effort, like every send here. The token exists only in this call: its sha256 is all that
    was persisted, and it is never returned to the HTTP caller — otherwise whoever guessed an
    address would hold the challenge for it."""
    try:
        notify.send(
            email, "Confirm your removal request",
            "Someone (probably you) asked us to remove records matching this email address.\n\n"
            f"Request id: {request_id}\nConfirmation code: {token}\n\n"
            "Enter both on the removal page to confirm. The code expires in 48 hours.\n"
            "If this wasn't you, ignore this message — nothing has changed or will change.")
    except Exception:  # noqa: BLE001
        _log.warning("repudiation challenge send failed", exc_info=True)


@router.post("/api/graph/repudiate", status_code=202)
def repudiate(body: dict, request: Request):
    """PUBLIC non-member data-subject request, A1 (a non-member has no account to sign into).

    Nothing is deleted here any more. An anonymous assertion is not authorization, so this route
    only ever ENQUEUES: an `email` starts a challenge (prove control of the address and the deletion
    becomes self-action), anything else queues a coordinator review of the asserted name.

    Both branches answer 202 with the same shape whether or not the details matched anything — and
    the email branch answers identically when a send cap silently swallowed the mail (security H3).
    A different status, body, or timing per branch would make this route a membership oracle, which
    is the whole thing the queue exists to avoid."""
    if not _repudiate_rate.allow(_client_key(request), time.time()):
        raise HTTPException(429, "Too many requests — try again shortly.")
    school_id = int(body.get("school_id") or 1)
    email = str(body.get("email") or "").strip()
    store = _network_store()
    try:
        if email:
            made = store.create_repudiation(school_id, kind="email_challenge", email=email)
            if made.get("email_token"):
                _send_repudiation_challenge(email, made["request_id"], made["email_token"])
            return {"status": "challenge_sent", "request_id": made["request_id"]}
        made = store.create_repudiation(
            school_id, kind="name_review", first=str(body.get("first") or ""),
            last=str(body.get("last") or ""), company=str(body.get("company") or ""))
    except GraphError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "queued_for_review", "request_id": made["request_id"]}


@router.post("/api/graph/repudiate/confirm")
def repudiate_confirm(body: dict, request: Request):
    """The email path's authorization step: request_id + address + emailed token. Only THIS route
    reaches a deletion executor, and only the email one (security L2)."""
    if not _repudiate_rate.allow(_client_key(request), time.time()):
        raise HTTPException(429, "Too many requests — try again shortly.")
    try:
        _network_store().confirm_repudiation(str(body.get("request_id") or ""),
                                             str(body.get("email") or ""),
                                             str(body.get("token") or ""))
    except GraphError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.get("/api/coordinator/repudiations")
def list_repudiations(status: str = "pending",
                      user: dict = Depends(require_role("coordinator", "admin"))):
    """The name-review queue, school-scoped from the SESSION (D13) — never from a query param.
    Rows carry a counts-only match preview so the decision isn't blind (security L2)."""
    return {"requests": _network_store().list_repudiations(user.get("school_id") or 1, status)}


@router.post("/api/coordinator/repudiations/{request_id}/decide")
def decide_repudiation(request_id: str, body: dict,
                       user: dict = Depends(require_role("coordinator", "admin"))):
    """Approve/deny a name review. The store's WHERE carries school_id, so another tenant's request
    is simply absent -> 404, never 403 (a 403 would confirm the id exists — security C1)."""
    try:
        return _network_store().decide_repudiation(
            user.get("school_id") or 1, request_id, user["id"], bool(body.get("approve")))
    except GraphError as exc:
        raise HTTPException(404, str(exc))


# ---- Slice AB: contacts import (202 + poll; consent-gated; no count egress) ------------------------
@router.post("/api/network/import", status_code=202)
async def import_contacts(request: Request, user: dict = Depends(require_role("student"))):
    if not _student_store().has_consent(user["id"], "contacts_upload"):
        raise HTTPException(409, "Grant the 'upload my contacts' consent first.")
    if not _extract_rate.allow(user["id"]):
        raise HTTPException(429, "Too many imports — wait a minute and try again.")
    form = await request.form()
    upload = form.get("contacts")
    if upload is None or not getattr(upload, "filename", ""):
        raise HTTPException(400, "Attach your Connections.csv (field name: contacts).")
    data = await upload.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "File too large (5 MB max).")
    import base64
    job_id = get_job_store().enqueue("resolve_network", {
        "user_id": user["id"], "school_id": user.get("school_id") or 1,
        "csv_b64": base64.b64encode(data).decode("ascii")}, owner_user_id=user["id"])
    return {"job_id": job_id, "poll": f"/api/jobs/{job_id}"}


# ---- Slice AD: pathfinder (bare boolean, gated behind an application) ------------------------------
def _hiring_manager(posting: dict) -> int | None:
    """The pathfinder's target. Preference order (C5): the posting's designated contact member,
    then the member behind its employer_contacts business contact, then the creator as fallback.

    created_by=0 is the ERASURE SENTINEL, not a person (§6 step 26): an erased employer's postings
    survive as org records with created_by=0, and targeting uid 0 would resurrect the erased human
    as a graph node. Map it — and NULL — to None; callers already 409 on a missing target (FM-M5)."""
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(pc.contact_user_id, ec.contact_user_id) AS uid FROM posting_contacts pc "
            "LEFT JOIN employer_contacts ec ON ec.id = pc.employer_contact_id "
            "WHERE pc.posting_id=? AND COALESCE(pc.contact_user_id, ec.contact_user_id) IS NOT NULL "
            "LIMIT 1", (posting["id"],)).fetchone()
    target = row["uid"] if row else posting.get("created_by")
    return target or None


def _broker_ok(store: StudentStore):
    """A2: a mutual who never granted `warm_intro` is not a broker, and a path through them must be
    pruned BEFORE ranking so the response is byte-identical to 'no path' — a shorter list or a
    lower score would turn the pathfinder into an oracle for who opted into brokering.

    Memoized per call: a bounded BFS revisits the same candidate many times."""
    cache: dict[int, bool] = {}

    def ok(uid: int) -> bool:
        if uid not in cache:
            cache[uid] = store.has_consent(uid, "warm_intro")
        return cache[uid]

    return ok


def _has_application(user_id: int, posting_id: str) -> bool:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT 1 FROM applications WHERE student_id=? AND posting_id=?",
                            (user_id, posting_id)).fetchone() is not None


@router.get("/api/intros/available/{posting_id}")
def intro_available(posting_id: str, user: dict = Depends(require_role("student"))):
    """Bare boolean, only after applying, only for a live posting in the caller's school
    (enumeration-oracle fix). A later silent decline is thus indistinguishable from 'no path'."""
    posting = _posting_store().get(posting_id)
    if posting is None or posting["status"] != "live" or not _can_view(posting, user):
        raise HTTPException(404, "No such posting.")
    if not _has_application(user["id"], posting_id):
        return {"warm_intro_available": False}   # gate: no probing before applying
    target = _hiring_manager(posting)
    if target is None or target == user["id"]:
        return {"warm_intro_available": False}
    paths = find_paths(_rel_store(), user["id"], target, user.get("school_id") or 1,
                       broker_ok=_broker_ok(_student_store()))
    return {"warm_intro_available": bool(paths)}


# ---- Slice AE: double-opt-in intro flow -----------------------------------------------------------
def _intro_read_access(intro_id: str, user: dict) -> dict:
    """READ access only (coordinator/admin, broker, or requester). Never used to gate mutations."""
    intro = _intro_store().get(intro_id)
    if intro is None:
        raise HTTPException(404, "No such intro request.")
    if user["role"] in ("coordinator", "admin") or user["id"] in (
            intro["broker_user_id"], intro["requester_user_id"]):
        return intro
    raise HTTPException(404, "No such intro request.")


@router.post("/api/intros/requests", status_code=201)
def create_intro(body: dict, user: dict = Depends(require_role("student"))):
    application_id = str(body.get("application_id") or "")
    apps = ApplicationStore()
    app_row = apps.get(application_id)
    if app_row is None or app_row["student_id"] != user["id"]:   # IDOR fix
        raise HTTPException(404, "No such application.")
    posting = _posting_store().get(app_row["posting_id"])
    if posting is None or posting["status"] != "live":
        raise HTTPException(404, "No such posting.")
    target = _hiring_manager(posting)
    if target is None or target == user["id"]:
        raise HTTPException(409, "No warm intro is available for this posting.")
    paths = find_paths(_rel_store(), user["id"], target, user.get("school_id") or 1,
                       broker_ok=_broker_ok(_student_store()))
    if not paths:
        raise HTTPException(409, "No warm intro is available for this posting.")
    note = redact_text(str(body.get("note") or "")[:500])
    school = user.get("school_id") or 1
    try:
        # C2: origin is metadata about the PATH ('did this lean on a bridge we manufactured?'),
        # never an attribute of the student. SM-M2: the binding broker-consent check is inside
        # create()'s transaction — the _broker_ok prune above is advisory (a broker can revoke in
        # the window between pathfinding and INSERT).
        created = _intro_store().create(
            school_id=school, posting_id=posting["id"],
            application_id=application_id, requester_user_id=user["id"], target_user_id=target,
            path=paths[0], note_redacted=note, origin=path_origin(paths[0]))
    except IntroError as exc:
        raise HTTPException(409, str(exc))
    _notify_user(paths[0]["broker"], school, "intro_request", "Someone asked you for an intro",
                 "A student you're connected to asked for a warm introduction. Open your intro "
                 "inbox to accept or decline.",
                 entity="intro", entity_id=created["intro_id"])
    return created


@router.get("/api/intros/inbox")
def intro_inbox(user: dict = Depends(require_role())):
    return {"requests": _intro_store().inbox(user["id"])}


@router.get("/api/intros/requests/mine")
def intro_mine(user: dict = Depends(require_role("student"))):
    return {"requests": _intro_store().mine(user["id"])}


@router.post("/api/intros/requests/{intro_id}/accept")
def accept_intro(intro_id: str, body: dict, user: dict = Depends(require_role())):
    intro = _intro_store().get(intro_id)
    if intro is None or intro["broker_user_id"] != user["id"]:   # CRITICAL: broker-only, explicit
        raise HTTPException(403, "Only the requested connection can accept this intro.")
    # the broker writes a job-related vouch about the requester; verify tier from broker role
    vouch = _rel_store().create_vouch(
        school_id=intro["school_id"], voucher_user_id=user["id"],
        subject_user_id=intro["requester_user_id"], relationship=str(body.get("relationship") or "other"),
        evidence=str(body.get("evidence") or ""), scope="posting", posting_id=intro["posting_id"],
        verify_level=_broker_verify_level(user))
    try:
        out = _intro_store().accept(intro_id, user["id"], vouch["vouch_id"])
    except IntroError as exc:
        raise HTTPException(409, str(exc))
    # accept is the ONLY side that notifies: a decline stays silent to the requester (D8), so a
    # declined intro remains indistinguishable from 'no path was ever found'.
    _notify_user(intro["requester_user_id"], intro["school_id"], "intro_accepted",
                 "Your intro request was accepted",
                 "A connection agreed to introduce you. Open your intro requests to see who.",
                 entity="intro", entity_id=intro_id)
    return out


@router.post("/api/intros/requests/{intro_id}/decline")
def decline_intro(intro_id: str, user: dict = Depends(require_role())):
    intro = _intro_store().get(intro_id)
    if intro is None or intro["broker_user_id"] != user["id"]:   # broker-only, explicit
        raise HTTPException(403, "Only the requested connection can decline this intro.")
    try:
        return _intro_store().decline(intro_id, user["id"])
    except IntroError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/intros/broker/block")
def broker_block(body: dict, user: dict = Depends(require_role())):
    _intro_store().block(user["id"], int(body.get("blocked_user_id") or 0))
    return {"ok": True}


# ---- Slice AF: vouches ----------------------------------------------------------------------------
@router.post("/api/vouches", status_code=201)
def create_vouch(body: dict, user: dict = Depends(require_role())):
    subject_id = int(body.get("subject_user_id") or 0)
    school = user.get("school_id") or 1
    try:
        vouch = _rel_store().create_vouch(
            school_id=school, voucher_user_id=user["id"],
            subject_user_id=subject_id,
            relationship=body.get("relationship"), evidence=str(body.get("evidence") or ""),
            scope=str(body.get("scope") or "general"), posting_id=body.get("posting_id"),
            org_id=int(body["org_id"]) if body.get("org_id") else None,   # B10: was dropped here
            verify_level="self")   # self-authored -> low weight until a coordinator verifies
    except RelationshipError as exc:
        raise HTTPException(400, str(exc))
    _notify_user(subject_id, school, "vouch_received", "Someone vouched for you",
                 "A new vouch about you was added. Open your reference ledger to review or "
                 "contest it.",
                 entity="vouch", entity_id=vouch.get("vouch_id"))
    return vouch


@router.get("/api/vouches/about-me")
def vouches_about_me(user: dict = Depends(require_role())):
    return {"vouches": _rel_store().vouches_about(user["id"])}


@router.post("/api/vouches/{vouch_id}/contest")
def contest_vouch(vouch_id: str, body: dict, user: dict = Depends(require_role())):
    try:
        out = _rel_store().contest_vouch(vouch_id, user["id"], str(body.get("note") or ""))
    except RelationshipError as exc:
        raise HTTPException(404, str(exc))
    school = user.get("school_id") or 1
    for uid in _school_coordinators(school):   # the queue they resolve (B10) — the note stays there
        _notify_user(uid, school, "vouch_contested", "A vouch was contested",
                     "A member disputed a vouch written about them. It's in the vouch review "
                     "queue.", entity="vouch", entity_id=vouch_id)
    return out


@router.post("/api/vouches/{vouch_id}/verify")
def verify_vouch(vouch_id: str, body: dict,
                 user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return _rel_store().verify_vouch(vouch_id, user["id"],
                                         str(body.get("verify_level") or "coordinator"))
    except RelationshipError as exc:
        raise HTTPException(400, str(exc))


# ---- Slice AH: warm-intro fairness report (aggregate-only, MIN_CELL=5) ----------------------------
def _refs(user_ids) -> list[str]:
    return [f"student-{uid}" for uid in user_ids]


@router.get("/api/coordinator/reports/intro-equity")
def intro_equity(format: str = "json",
                 user: dict = Depends(require_role("coordinator", "admin"))):
    """Does warm-intro ACCESS/CONVERSION concentrate among privileged self-ID groups? Computed from
    TWO INDEPENDENT AuditDB.aggregate() calls per attribute (denominator=all applicants,
    numerator=intro receivers / converters) — never an aligned per-person label list. The scoring
    plane supplies only opaque refs; the audit DB answers with min-cell-suppressed counts.

    A7: every cohort is filtered to students holding `network_analytics` — the consent whose whole
    point is "you may count me in fairness/overlap aggregates". Shipping this report over the
    non-consenting was the promise this route had been breaking.
    A8: served through the snapshot policy, so two reads can't be differenced across a joiner."""
    from ..audit.metrics import access_disparity
    from ..stores.audit_store import AuditDB
    from ..stores.data_planes import AUDITABLE_ATTRIBUTES

    school = user.get("school_id") or 1
    with closing(db_connect()) as conn:
        applicants = [r["student_id"] for r in conn.execute(
            "SELECT DISTINCT a.student_id FROM applications a JOIN postings p ON p.id=a.posting_id "
            "WHERE p.school_id=?", (school,))]
        requested = [r["requester_user_id"] for r in conn.execute(
            "SELECT DISTINCT requester_user_id FROM intro_requests WHERE school_id=?", (school,))]
        converted = [r["requester_user_id"] for r in conn.execute(
            "SELECT DISTINCT requester_user_id FROM intro_requests WHERE school_id=? "
            "AND status='accepted'", (school,))]
    students = _student_store()
    applicants = students.filter_by_consent(applicants, "network_analytics")
    requested = students.filter_by_consent(requested, "network_analytics")
    converted = students.filter_by_consent(converted, "network_analytics")

    def _compute() -> dict:
        audit = AuditDB()
        by_attr = {}
        for attr in sorted(AUDITABLE_ATTRIBUTES):
            denom = audit.aggregate(_refs(applicants), attr)["counts"]
            by_attr[attr] = {
                "access": access_disparity(audit.aggregate(_refs(requested), attr)["counts"],
                                           denom),
                "conversion": access_disparity(audit.aggregate(_refs(converted), attr)["counts"],
                                               denom),
            }
        return {"applicants": len(applicants), "requested": len(requested),
                "converted": len(converted), "by_attribute": by_attr}

    payload = _serve_snapshot("intro_equity", school, len(applicants), _compute)
    report = payload["by_attribute"]
    if format == "csv":
        import csv
        import io

        from fastapi.responses import PlainTextResponse
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["attribute", "funnel", "min_impact_ratio", "four_fifths_pass"])
        for attr, funnels in report.items():
            for name, d in funnels.items():
                w.writerow([attr, name, d["min_impact_ratio"], d["four_fifths_pass"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return payload


# ---- Slice AI: mitigation coverage + coordinator-initiated bridge (governed positive action) ------
@router.get("/api/coordinator/reports/network-coverage")
def network_coverage(user: dict = Depends(require_role("coordinator", "admin"))):
    """Structural under-networking (network_poverty = a discoverable student with ZERO shareable
    edges) and how many got a coordinator/alumni bridge. NEVER keyed on self-ID — the trigger is
    structural. This is the shut-off dashboard for the positive-action program.

    A7/FH-H3: this report was the one A7 forgot. Counting a student's network position is analytics
    ABOUT that student, so the whole cohort — discoverable AND bridged — is filtered to holders of
    `network_analytics`; `graph_discoverable` alone is consent to be FOUND, not to be measured."""
    school = user.get("school_id") or 1
    now = time.time()
    with closing(db_connect()) as conn:
        discoverable = [r["user_id"] for r in conn.execute(
            "SELECT u.id AS user_id FROM users u WHERE u.school_id=? AND u.role='student' "
            "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=u.id "
            "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL)", (school,))]
        accepted = [r["requester_user_id"] for r in conn.execute(
            "SELECT DISTINCT requester_user_id FROM intro_requests WHERE school_id=? "
            "AND status='accepted'", (school,))]
    students = _student_store()
    discoverable = students.filter_by_consent(discoverable, "network_analytics")
    bridged = len(students.filter_by_consent(accepted, "network_analytics"))
    under = 0
    with closing(db_connect()) as conn:
        for uid in discoverable:
            deg = conn.execute(
                "SELECT COUNT(*) FROM graph_edges ge WHERE ge.school_id=? "
                "AND (ge.user_a=? OR ge.user_b=?) AND ge.consent_state='shareable' "
                "AND ge.revoked_at IS NULL AND (ge.expires_at IS NULL OR ge.expires_at > ?)",
                (school, uid, uid, now)).fetchone()[0]
            if deg == 0:
                under += 1
    return {"discoverable_students": len(discoverable), "network_poverty": under,
            "students_with_accepted_intro": bridged,
            "note": "network_poverty is structural (zero shareable edges), never self-ID-based; "
                    "cohort = graph_discoverable AND network_analytics consent"}


@router.post("/api/coordinator/intros/bridge")
def coordinator_bridge(body: dict, user: dict = Depends(require_role("coordinator", "admin"))):
    """A coordinator manufactures a warm path for an under-networked student by creating a
    verified alumni/coordinator vouch edge to a willing mentor (who must hold warm_intro consent).
    This is the active-mitigation lever; the mentor still opts in via their standing consent."""
    student_id = int(body.get("student_id") or 0)
    mentor_id = int(body.get("mentor_id") or 0)
    if not student_id or not mentor_id:
        raise HTTPException(400, "student_id and mentor_id are required.")
    if not _student_store().has_consent(mentor_id, "warm_intro"):
        raise HTTPException(409, "That mentor hasn't opted into making intros.")
    school = user.get("school_id") or 1
    with closing(db_connect()) as conn:
        _rel_store().upsert_edge(conn, school, student_id, mentor_id, "alumni_bridge",
                                 provenance="alumni", consent_state="pending")
        conn.commit()
    _rel_store().promote_shareable(school)
    return {"ok": True, "bridged": True}


# ---- Slice AG: employer evidence card -------------------------------------------------------------
@router.get("/api/intros/for-application/{application_id}")
def intro_card(application_id: str,
               user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    app_row = ApplicationStore().get(application_id)
    if app_row is None:
        raise HTTPException(404, "No such application.")
    posting = _posting_store().get(app_row["posting_id"])
    _require_own_org_posting(posting, user)
    # RELATIONSHIPS.md:395 — an evidence-card view is a human seeing relationship evidence about a
    # candidate, i.e. the same AEDT-relevant moment the shortlist view logs. Dedupe is inside.
    _record_exposure(user, app_row["posting_id"])
    intros = _intro_store().accepted_for_application(application_id)
    vouches = _rel_store().vouches_for_subject_on_posting(app_row["student_id"],
                                                          app_row["posting_id"])
    # quoted, attributable, job-related evidence — NEVER blended into match_results / fit_score
    return {"claim_kind": "job_related_evidence_not_hire_recommendation",
            "warm_intros": [{"broker_role": i["broker_role"], "hops": i["hops"]} for i in intros],
            "vouches": vouches}


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
