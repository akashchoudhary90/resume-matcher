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

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..antigaming.injection import scan_injection
from ..antigaming.keyword_stuffing import scan_keyword_stuffing
from ..ingestion.job_posting import build_job_spec, detect_job_skills, skill_options
from ..ingestion.parser import (
    ParseError,
    SUPPORTED_EXTS,
    infer_education_level,
    infer_years_experience,
    parse_resume_bytes,
)
from ..inference.adapter import get_adapter
from ..inference.adapters import claude_cli as _claude_cli
from ..inference.schema import CandidateProfile
from ..matching import coaching as coaching_mod
from ..matching import ranker
from ..matching.taxonomy import canonical_name, normalize_skills
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
# Matching engine: "claude_cli" (Claude on your subscription via the local Claude Code CLI — no API
# key; DEFAULT) or "mock" (deterministic). claude_cli falls back to mock when the CLI/token is absent,
# so this is safe to default on even before the token is configured.
DEMO_BACKEND = os.environ.get("RM_DEMO_BACKEND", "claude_cli")
CONCURRENCY = max(1, _int_env("RM_DEMO_CONCURRENCY", 4))  # parallel extractions per upload batch
# With the Claude engine, send PDFs/images to Claude directly (vision) instead of extracting text
# first — reads scanned resumes + preserves layout. Set RM_DEMO_SEND_FILE=0 to force text extraction.
SEND_FILE = os.environ.get("RM_DEMO_SEND_FILE", "1").lower() in ("1", "true", "yes")
CACHE_MAX = _int_env("RM_DEMO_CACHE_MAX", 512)  # in-memory extraction cache (consistency + speed)

# Cache the LLM EXTRACTION keyed by (content, job, model). The LLM is non-deterministic, so this makes
# re-scoring the SAME resume against the SAME job IDENTICAL (and instant). In-memory only (no disk),
# bounded, cleared on restart — consistent with the ephemeral privacy posture.
_EXTRACT_CACHE: dict[str, object] = {}
_CACHE_LOCK = threading.Lock()


def _job_signature(job) -> str:
    return json.dumps(
        {"r": sorted(job.required_skills), "p": sorted(job.preferred_skills),
         "m": sorted(job.must_have_skills), "y": job.min_years, "e": job.min_education},
        sort_keys=True,
    )


def _cache_key(kind: str, model: str, job_sig: str, content: bytes) -> str:
    h = hashlib.sha256()
    for part in (kind.encode(), model.encode(), job_sig.encode()):
        h.update(part)
        h.update(b"\x00")
    h.update(content)
    return h.hexdigest()


def _cache_get(key: str):
    with _CACHE_LOCK:
        return _EXTRACT_CACHE.get(key)


def _cache_put(key: str, value) -> None:
    with _CACHE_LOCK:
        if len(_EXTRACT_CACHE) >= CACHE_MAX:
            _EXTRACT_CACHE.clear()
        _EXTRACT_CACHE[key] = value


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
    engine: str = "mock"  # which matching engine actually ran ("mock" | "claude_cli")
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
            "engine": self.engine,
            "score_kind": "fit_readiness_not_hire_probability",
            "privacy": {
                "stored_on_disk": False,
                "raw_text_retained": False,
                "ttl_minutes": round(self.ttl_seconds / 60),
                "seconds_until_auto_delete": max(0, int(self.expires_at - now)),
                "note": (
                    "Your resumes were processed transiently and are not stored: the full resume "
                    "text is discarded right after scoring (only the score breakdown with short "
                    "quotes is kept). When NDR AI reads a PDF/image directly, a temporary copy is "
                    "written so the AI engine can open it, then deleted immediately. This "
                    "session auto-deletes when idle and you can delete it now; a restart erases all."
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

    def create(self, job: dict, results: list[dict], n_resumes: int, warnings: list[str],
               engine: str = "mock") -> DemoSession:
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
            engine=engine,
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


def _candidate_from_text(cid: str, text: str) -> CandidateProfile:
    """Build a CandidateProfile from already-clean text (e.g. Claude's file transcription)."""
    return CandidateProfile(
        candidate_id=cid,
        text=text,
        skills=normalize_skills(text),
        education_level=infer_education_level(text),
        years_experience=infer_years_experience(text),
        has_resume=bool(text.strip()),
    )


def run_demo(
    *,
    store: SessionStore,
    job_text: str = "",
    title: str = "",
    employer: str = "",
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    must_have_skills: list[str] | None = None,
    min_education: str | None = None,
    min_years: float | None = None,
    files: list[tuple[str, bytes]],
    backend: str | None = None,
) -> DemoSession:
    """Parse uploads in memory, score them against the job, store ONLY the de-identified results.

    `files` is a list of (filename, raw_bytes). Returns the created DemoSession. The raw bytes and
    parsed resume text exist only as locals here and are discarded on return."""
    validate_uploads(files)
    if not (job_text or "").strip() and not (required_skills or preferred_skills or must_have_skills):
        raise DemoError("Paste a job posting or provide at least one required skill.")

    # If no explicit skills were tagged, auto-detect them from the pasted posting (so skipping the
    # "Detect skills" step still yields a meaningful match) and infer the minimum education.
    if not required_skills and not preferred_skills and not must_have_skills and (job_text or "").strip():
        required_skills = detect_job_skills(job_text)
        if min_education is None:
            min_education = infer_education_level(job_text)

    job = build_job_spec(
        job_id="DEMO_JOB",
        title=title,
        employer=employer,
        description=job_text or "",
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        must_have_skills=must_have_skills,
        min_education=min_education,
        min_years=min_years,
    )

    # Without any job skills, every resume scores 0 — that's not a useful result. Refuse with a clear
    # message instead of silently returning zeros (this is what produced the confusing 0/grade-D).
    if not job.required_skills and not job.preferred_skills:
        raise DemoError(
            "No job skills to match against — every resume would score 0. Paste the job posting and "
            "click 'Detect skills', or add at least one required skill. (If the posting doesn't "
            "mention skills our matcher recognizes, add them manually.)"
        )

    # Assign a candidate id + display label per upload up front.
    items: list[tuple[str, str, str, bytes]] = []
    seen_labels: set[str] = set()
    warnings: list[str] = []
    for idx, (filename, data) in enumerate(files):
        label = _label_for(filename, idx)
        if label in seen_labels:  # disambiguate identical filenames
            label = f"{label} ({idx + 1})"
        seen_labels.add(label)
        items.append((f"R{idx + 1:02d}", label, filename, data))

    adapter, engine = _resolve_adapter(backend or DEMO_BACKEND)
    # File-direct (Claude reads the actual PDF/image) is on only when the Claude engine is active.
    send_file = SEND_FILE and engine == "claude_cli"
    model = _claude_cli.model_name() if engine == "claude_cli" else engine
    job_sig = _job_signature(job)

    def _text_extraction(cand: CandidateProfile):
        """Extraction for a text candidate, cached (consistent + fast); fail-quiet to mock."""
        key = _cache_key("text", model, job_sig, cand.text.encode("utf-8"))
        cached = _cache_get(key)
        if cached is not None:
            return cached
        try:
            ex = adapter.extract(cand, job)
        except Exception:  # noqa: BLE001 - per-candidate fail-quiet to the deterministic engine
            ex = get_adapter("mock").extract(cand, job)
        _cache_put(key, ex)
        return ex

    def _score_upload(item):
        """Return (ScoreResult|None, CandidateProfile|None, label, note|None)."""
        cid, label, filename, data = item
        ext = os.path.splitext(filename or "")[1].lower()
        note = None
        # --- File-direct: Claude reads the actual PDF/image (best fidelity, reads scans) ---
        if send_file and _claude_cli.supports_file(filename or ""):
            key = _cache_key("file", model, job_sig, data)
            cached = _cache_get(key)
            tmp = None
            try:
                if cached is not None:
                    resume_text, extraction = cached  # identical re-score, no LLM call
                else:
                    fd, tmp = tempfile.mkstemp(suffix=ext or ".pdf")
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)  # written briefly so the CLI can read it; deleted in finally
                    resume_text, extraction = _claude_cli.extract_from_file(tmp, job, cid)
                    _cache_put(key, (resume_text, extraction))
                if resume_text.strip():
                    cand = _candidate_from_text(cid, resume_text)
                    flags = scan_injection(resume_text) + scan_keyword_stuffing(resume_text, job)
                    return ranker.score(extraction, cand, job, extra_flags=flags), cand, label, None
                note = f"{label}: NDR AI read no text from the file; tried local text extraction."
            except Exception as exc:  # noqa: BLE001 - fall back to text extraction
                note = f"{label}: NDR AI file-read failed ({type(exc).__name__}); used text extraction."
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        # --- Text path (default for .txt/.docx; also the fallback for the above) ---
        try:
            cand = parse_resume_bytes(cid, filename or f"{cid}.txt", data, redact=False)
        except ParseError as exc:
            return None, None, label, f"{label}: {exc}"
        if not cand.text.strip():
            return None, None, label, note or (
                f"{label}: no readable text. If it's a scanned/photo PDF, the NDR AI engine can read "
                f"it — otherwise upload a text-based PDF, a .docx, or a .txt."
            )
        flags = scan_injection(cand.text) + scan_keyword_stuffing(cand.text, job)
        return ranker.score(_text_extraction(cand), cand, job, extra_flags=flags), cand, label, note

    workers = min(len(items), CONCURRENCY) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(_score_upload, items))

    scored = []
    for res, cand, label, note in outcomes:
        if note:
            warnings.append(note)
        if res is not None:
            scored.append((res, cand, label))

    if not scored:
        raise DemoError("None of the uploaded files could be scored. " + " ".join(warnings[-3:]))

    job_skill_set = set(job.required_skills) | set(job.preferred_skills)
    scored.sort(key=lambda t: t[0].fit_score, reverse=True)
    results = []
    for res, cand, label in scored:
        row = result_to_dict(res, coaching_mod.coach(res, job), label=label)
        if cand is not None:
            row["education_level"] = cand.education_level
            row["years_experience"] = cand.years_experience
            row["skills_found"] = len(cand.skills)
            # Skills the candidate has that the job didn't ask for (the "also brings" half of the gap view).
            row["extra_skills"] = [canonical_name(s) for s in cand.skills if s not in job_skill_set]
        results.append(row)

    must_set = set(job.must_have_skills)
    job_summary = {
        "title": job.title,
        "employer": job.employer,
        # required excludes must-haves here so the UI can show them as their own tier
        "required_skills": skill_options([s for s in job.required_skills if s not in must_set]),
        "preferred_skills": skill_options(job.preferred_skills),
        "must_have_skills": skill_options(job.must_have_skills),
        "min_education": job.min_education,
        "min_years": job.min_years,
    }
    # Local resume text / bytes go out of scope here and are garbage-collected; only `results`
    # (the score breakdown) is persisted in the session.
    return store.create(
        job=job_summary, results=results, n_resumes=len(scored), warnings=warnings, engine=engine
    )


def _resolve_adapter(name: str):
    """Return (adapter, engine_name). If the Claude backend is requested but unavailable (CLI/token
    missing), fall back to the deterministic mock so the demo always works — the `engine` field in the
    result tells the operator which engine actually ran (no scary banner for the client)."""
    if name == "claude_cli" and not _claude_cli.available():
        return get_adapter("mock"), "mock"
    return get_adapter(name), name


_STORE: SessionStore | None = None


def get_demo_store() -> SessionStore:
    """Process-wide singleton store, mirroring service.get_state()."""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE
