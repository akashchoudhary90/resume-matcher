"""Ephemeral, in-memory demo flow: 1 job posting + up to N resumes, scored and then forgotten.

Privacy design (locked decisions, 2026-06-20):
  * Uploaded resume bytes are parsed ENTIRELY IN MEMORY and never written to disk.
  * The full resume text is DROPPED the instant scoring finishes — a session keeps only the
    de-identified score breakdown (with short, already-redacted evidence quotes), never the resume.
  * Sessions auto-expire after an idle TTL (RM_DEMO_TTL_MINUTES, default 30) and a client can wipe
    its data immediately with the explicit DELETE endpoint ("Delete my data now").
  * The whole store is process memory only; a restart (or `docker compose down`) loses everything.

This module owns the SessionStore and the run_demo() orchestration. The FastAPI wiring lives in
app.py; the matching itself reuses the same deterministic pipeline as the synthetic dashboard.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from ..ingestion.job_posting import build_job_spec, skill_options
from ..ingestion.parser import ParseError, SUPPORTED_EXTS, parse_resume_bytes
from ..inference.adapter import get_adapter
from ..matching.pipeline import run_matching
from .serialize import result_to_dict


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


MAX_RESUMES = _int_env("RM_DEMO_MAX_RESUMES", 10)
MAX_FILE_MB = _int_env("RM_DEMO_MAX_FILE_MB", 4)
TTL_MINUTES = _int_env("RM_DEMO_TTL_MINUTES", 30)
MAX_SESSIONS = _int_env("RM_DEMO_MAX_SESSIONS", 100)
DEMO_BACKEND = os.environ.get("RM_DEMO_BACKEND", "mock")  # deterministic + fully local by default


class DemoError(Exception):
    """A client-correctable problem (too many files, empty job, etc.) -> HTTP 400."""


@dataclass
class DemoSession:
    session_id: str
    created_at: float
    last_seen: float
    ttl_seconds: int
    job: dict
    results: list[dict] = field(default_factory=list)
    n_resumes: int = 0
    warnings: list[str] = field(default_factory=list)
    # NB: raw resume text is intentionally NOT a field here — it is dropped after scoring.

    @property
    def expires_at(self) -> float:
        return self.last_seen + self.ttl_seconds

    def to_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "session_id": self.session_id,
            "job": self.job,
            "results": self.results,
            "n_resumes": self.n_resumes,
            "warnings": self.warnings,
            "score_kind": "fit_readiness_not_hire_probability",
            "privacy": {
                "stored_on_disk": False,
                "raw_text_retained": False,
                "ttl_minutes": round(self.ttl_seconds / 60),
                "seconds_until_auto_delete": max(0, int(self.expires_at - now)),
                "note": (
                    "Your resumes were processed in memory only — never written to disk — and the "
                    "full resume text was discarded right after scoring (only the score breakdown "
                    "with short quotes is kept). This session auto-deletes when idle and you can "
                    "delete it now with the button below. A server restart also erases everything."
                ),
            },
        }


class SessionStore:
    """Thread-safe, in-memory, TTL-bounded store of demo sessions."""

    def __init__(self, ttl_seconds: int | None = None, max_sessions: int = MAX_SESSIONS) -> None:
        self.ttl_seconds = TTL_MINUTES * 60 if ttl_seconds is None else ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, DemoSession] = {}
        self._lock = threading.Lock()

    def create(self, job: dict, results: list[dict], n_resumes: int, warnings: list[str]) -> DemoSession:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        sess = DemoSession(
            session_id=sid,
            created_at=now,
            last_seen=now,
            ttl_seconds=self.ttl_seconds,
            job=job,
            results=results,
            n_resumes=n_resumes,
            warnings=warnings,
        )
        with self._lock:
            self._evict_if_needed_locked(now)
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> DemoSession | None:
        now = time.time()
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if now - sess.last_seen > self.ttl_seconds:
                self._purge_locked(sid)
                return None
            sess.last_seen = now  # idle TTL: any access extends the window
            return sess

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._purge_locked(sid)

    def sweep(self) -> int:
        """Purge every expired session. Returns how many were removed."""
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if now - s.last_seen > self.ttl_seconds]
            for sid in stale:
                self._purge_locked(sid)
            return len(stale)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ---- internals (call with the lock held) -------------------------------------------------
    def _purge_locked(self, sid: str) -> bool:
        sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        # Best-effort scrub so references don't linger in memory after removal.
        sess.results = []
        sess.job = {}
        return True

    def _evict_if_needed_locked(self, now: float) -> None:
        if len(self._sessions) < self.max_sessions:
            return
        # Drop the least-recently-seen session to make room (also clears anything expired).
        oldest = min(self._sessions.values(), key=lambda s: s.last_seen, default=None)
        if oldest is not None:
            self._purge_locked(oldest.session_id)


def validate_uploads(files: list[tuple[str, bytes]]) -> None:
    """Reject obviously bad upload sets early, with client-correctable messages."""
    if not files:
        raise DemoError("Upload at least one resume.")
    if len(files) > MAX_RESUMES:
        raise DemoError(f"Too many resumes: {len(files)} (max {MAX_RESUMES}).")
    limit = MAX_FILE_MB * 1024 * 1024
    for name, data in files:
        if len(data) > limit:
            raise DemoError(f"'{name}' is larger than the {MAX_FILE_MB} MB limit.")


def _label_for(filename: str, idx: int) -> str:
    """Human-friendly label = the uploaded filename (without extension), which usually identifies the
    candidate. Clients consent to this PII; the session is still ephemeral and deletable."""
    stem = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0].strip()
    return stem or f"Resume {idx + 1}"


def run_demo(
    *,
    store: SessionStore,
    job_text: str = "",
    title: str = "",
    employer: str = "",
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    min_education: str | None = None,
    files: list[tuple[str, bytes]],
    backend: str | None = None,
) -> DemoSession:
    """Parse uploads in memory, score them against the job, store ONLY the de-identified results.

    `files` is a list of (filename, raw_bytes). Returns the created DemoSession. The raw bytes and
    parsed resume text exist only as locals here and are discarded on return."""
    validate_uploads(files)
    if not (job_text or "").strip() and not (required_skills or preferred_skills):
        raise DemoError("Paste a job posting or provide at least one required skill.")

    job = build_job_spec(
        job_id="DEMO_JOB",
        title=title,
        employer=employer,
        description=job_text or "",
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        min_education=min_education,
    )

    candidates = []
    labels: dict[str, str] = {}
    seen_labels: set[str] = set()
    warnings: list[str] = []
    for idx, (filename, data) in enumerate(files):
        cid = f"R{idx + 1:02d}"
        label = _label_for(filename, idx)
        if label in seen_labels:  # disambiguate identical filenames
            label = f"{label} ({idx + 1})"
        seen_labels.add(label)
        labels[cid] = label
        try:
            # Clients consent to PII, so names are kept (auto_redact_name=False) for identifiable
            # results; contact identifiers are still redacted at ingestion.
            cand = parse_resume_bytes(cid, filename or f"{cid}.txt", data, auto_redact_name=False)
        except ParseError as exc:
            warnings.append(f"{label}: {exc}")
            continue
        if not cand.text.strip():
            warnings.append(f"{label}: no readable text found (scanned image or empty file?).")
            continue
        candidates.append(cand)

    if not candidates:
        raise DemoError(
            "None of the uploaded files yielded readable text. "
            + (" ".join(warnings) if warnings else "")
        )

    adapter = get_adapter(backend or DEMO_BACKEND)
    n = len(candidates)
    run = run_matching(candidates, [job], adapter=adapter, retrieve_k=n, shortlist_k=n)
    shortlist = run.shortlists[0]

    by_id = {c.candidate_id: c for c in candidates}
    results = []
    for res, coach in zip(shortlist.ranked, shortlist.coaching):
        row = result_to_dict(res, coach, label=labels.get(res.candidate_id))
        cand = by_id.get(res.candidate_id)
        if cand is not None:
            row["education_level"] = cand.education_level
            row["years_experience"] = cand.years_experience
            row["skills_found"] = len(cand.skills)
        results.append(row)

    job_summary = {
        "title": job.title,
        "employer": job.employer,
        "required_skills": skill_options(job.required_skills),
        "preferred_skills": skill_options(job.preferred_skills),
        "min_education": job.min_education,
    }
    # Local resume text / bytes go out of scope here and are garbage-collected; only `results`
    # (de-identified breakdown) is persisted in the session.
    return store.create(job=job_summary, results=results, n_resumes=n, warnings=warnings)


_STORE: SessionStore | None = None


def get_demo_store() -> SessionStore:
    """Process-wide singleton store, mirroring service.get_state()."""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE
