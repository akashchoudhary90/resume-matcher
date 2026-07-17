"""Student-side stores (docs/IMPLEMENTATION.md Slices I/J): profile, consent lifecycle, the one
active resume, and the application pipeline.

Consent is the pool gate: `matchable_students()` returns ONLY students who are visible, hold an
active `profile_matching` consent, and have a resume — the filter applies BEFORE retrieval ever
sees a candidate (docs/PLATFORM.md graft #3). Resume delete is a HARD delete (blob + text gone in
one statement); replacing a resume hard-deletes the old row first.

Resume text is redacted at ingest via the same chokepoint as everywhere else
(ingestion/parser.parse_resume_bytes → redact_text): `redacted_text` is the only free text the
matching adapter will ever see (boundary #3).
"""
from __future__ import annotations

import secrets
import time
from contextlib import closing

from ..ingestion.parser import parse_resume_bytes
from .db import connect, migrate, platform_db_path

CONSENT_PURPOSES = (
    "resume_storage", "profile_matching", "self_id_audit", "contact",
    # Phase 4 (relationship graph) — must match the CHECK in migrations/003_phase4.sql:
    "contacts_upload",      # upload my OWN contacts export
    "graph_discoverable",   # be a node in pathfinding / be resolved as a mutual
    "warm_intro",           # a mutual may request a double-opt-in intro TO me
    "network_analytics",    # anonymized participation in fairness/overlap aggregates
)

# Forward-only application transitions (employer/coordinator move them; students only apply).
# 'withdrawn' (Phase 5 B7) is terminal and student-initiated ONLY — set_status refuses it so an
# employer can never withdraw on a student's behalf; students go through withdraw().
_APP_TRANSITIONS = {
    "applied": {"shortlisted", "advanced", "rejected", "hired", "withdrawn"},
    "shortlisted": {"advanced", "rejected", "hired", "withdrawn"},
    "advanced": {"rejected", "hired", "withdrawn"},
}


class StudentError(Exception):
    """Client-correctable problem -> HTTP 400/409 at the route."""


class StudentStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    # ---- profile ---------------------------------------------------------------------------------
    def upsert_profile(self, user_id: int, *, program: str = "", grad_year: int | None = None,
                       work_auth_simple: str = "", visibility: bool = True,
                       school_id: int = 1) -> dict:
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO student_profiles(user_id, school_id, program, grad_year, "
                "work_auth_simple, visibility, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET program=excluded.program, "
                "grad_year=excluded.grad_year, work_auth_simple=excluded.work_auth_simple, "
                "visibility=excluded.visibility, updated_at=excluded.updated_at",
                (user_id, school_id, (program or "").strip()[:120], grad_year,
                 (work_auth_simple or "").strip()[:120], int(bool(visibility)), time.time()),
            )
            conn.commit()
        return self.get_profile(user_id)

    def get_profile(self, user_id: int) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM student_profiles WHERE user_id=?",
                               (user_id,)).fetchone()
        return dict(row) if row else None

    # ---- consents (grant = append a row; revoke = stamp the active rows) --------------------------
    def set_consent(self, user_id: int, purpose: str, granted: bool) -> None:
        if purpose not in CONSENT_PURPOSES:
            raise StudentError(f"Unknown consent purpose {purpose!r}.")
        now = time.time()
        with closing(self._conn()) as conn:
            if granted:
                if not conn.execute(
                    "SELECT 1 FROM consents WHERE user_id=? AND purpose=? AND revoked_at IS NULL",
                    (user_id, purpose),
                ).fetchone():
                    conn.execute(
                        "INSERT INTO consents(user_id, purpose, granted_at) VALUES(?,?,?)",
                        (user_id, purpose, now),
                    )
            else:
                conn.execute(
                    "UPDATE consents SET revoked_at=? WHERE user_id=? AND purpose=? "
                    "AND revoked_at IS NULL",
                    (now, user_id, purpose),
                )
            conn.commit()

    def has_consent(self, user_id: int, purpose: str) -> bool:
        with closing(self._conn()) as conn:
            return conn.execute(
                "SELECT 1 FROM consents WHERE user_id=? AND purpose=? AND revoked_at IS NULL",
                (user_id, purpose),
            ).fetchone() is not None

    def consents(self, user_id: int) -> dict[str, bool]:
        return {p: self.has_consent(user_id, p) for p in CONSENT_PURPOSES}

    def filter_by_consent(self, user_ids: list[int], purpose: str) -> list[int]:
        """Keep only ids holding an active consent for `purpose` (A7 cohort filter); one IN(...)
        query, input order preserved."""
        if not user_ids:
            return []
        placeholders = ",".join("?" * len(user_ids))
        with closing(self._conn()) as conn:
            ok = {r[0] for r in conn.execute(
                f"SELECT DISTINCT user_id FROM consents WHERE user_id IN ({placeholders}) "
                "AND purpose=? AND revoked_at IS NULL",
                (*user_ids, purpose),
            )}
        return [uid for uid in user_ids if uid in ok]

    # ---- alumni (Phase 5 C4: users.alumni_status attribute, never a role) -------------------------
    def set_alumni_status(self, user_id: int, school_id: int, status: str,
                          attested_by: int | None = None) -> None:
        """School-scoped (D13): a cross-tenant user_id looks absent (route answers 404)."""
        if status not in ("none", "self_claimed", "verified"):
            raise StudentError(f"Unknown alumni status {status!r}.")
        with closing(self._conn()) as conn:
            cur = conn.execute("UPDATE users SET alumni_status=? WHERE id=? AND school_id=?",
                               (status, user_id, school_id))
            if not cur.rowcount:
                raise StudentError("No such user.")
            if attested_by is not None and status == "verified":
                # the coordinator's out-of-band SIS check; this append-only row IS the record
                conn.execute(
                    "INSERT INTO events(actor_user_id, action, entity, entity_id, at) "
                    "VALUES(?,?,?,?,?)",
                    (attested_by, "alumni_verified", "user", str(user_id), time.time()),
                )
            conn.commit()

    def alumni_queue(self, school_id: int) -> list[dict]:
        """Coordinator verification queue: self-claimed alumni in THIS school. Deliberately no
        grad_year anywhere near this query (privacy F2 — coordinators verify by email vs SIS)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT u.id AS user_id, u.email, p.program FROM users u "
                "LEFT JOIN student_profiles p ON p.user_id = u.id "
                "WHERE u.school_id=? AND u.alumni_status='self_claimed' ORDER BY u.id",
                (school_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- resume (one active per student; hard delete honored) -------------------------------------
    def save_resume(self, user_id: int, filename: str, content_type: str, data: bytes,
                    school_id: int = 1) -> dict:
        if not self.has_consent(user_id, "resume_storage"):
            raise StudentError("Grant the resume-storage consent before uploading a resume.")
        if self.get_profile(user_id) is None:
            # A resume without a saved profile must still land in the match pool — create the
            # default (visible) profile row AT THE STUDENT'S SCHOOL rather than excluding them.
            self.upsert_profile(user_id, school_id=school_id)
        profile = parse_resume_bytes(f"u{user_id}", filename, data)  # in-memory; redacts contacts
        if not profile.has_resume:
            raise StudentError("No readable text found in that file.")
        resume_id = secrets.token_urlsafe(10)
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM resumes WHERE user_id=?", (user_id,))  # replace = hard delete
            conn.execute(
                "INSERT INTO resumes(id, user_id, filename, content_type, file_blob, "
                "extracted_text, redacted_text, uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
                (resume_id, user_id, filename[:200], content_type, data,
                 None, profile.text, time.time()),
            )
            conn.commit()
        return {"resume_id": resume_id, "filename": filename,
                "skills_detected": len(profile.skills),
                "education_level": profile.education_level,
                "years_experience": profile.years_experience}

    def delete_resume(self, user_id: int) -> bool:
        with closing(self._conn()) as conn:
            cur = conn.execute("DELETE FROM resumes WHERE user_id=?", (user_id,))
            conn.commit()
            return cur.rowcount > 0

    def resume_meta(self, user_id: int) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT id, filename, content_type, uploaded_at FROM resumes WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    # ---- the match pool (consent + visibility filter BEFORE retrieval) ----------------------------
    def matchable_students(self, school_id: int = 1) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT r.user_id AS user_id, r.id AS resume_id, r.redacted_text AS redacted_text "
                "FROM resumes r JOIN student_profiles p ON p.user_id = r.user_id "
                "WHERE p.visibility=1 AND p.school_id=? AND EXISTS("
                "  SELECT 1 FROM consents c WHERE c.user_id = r.user_id "
                "  AND c.purpose='profile_matching' AND c.revoked_at IS NULL)",
                (school_id,),
            ).fetchall()
        return [dict(r) for r in rows]


class ApplicationStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    def apply(self, posting_id: str, student_id: int, resume_id: str | None) -> str:
        app_id = secrets.token_urlsafe(10)
        now = time.time()
        with closing(self._conn()) as conn:
            try:
                conn.execute(
                    "INSERT INTO applications(id, posting_id, student_id, resume_id, created_at, "
                    "updated_at) VALUES(?,?,?,?,?,?)",
                    (app_id, posting_id, student_id, resume_id, now, now),
                )
            except Exception as exc:  # UNIQUE(posting_id, student_id)
                raise StudentError("You already applied to this posting.") from exc
            conn.commit()
        return app_id

    def get(self, app_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def for_student(self, student_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT a.id, a.posting_id, p.title, p.status AS posting_status, a.status, "
                "a.human_review_requested, a.created_at, a.updated_at "
                "FROM applications a JOIN postings p ON p.id = a.posting_id "
                "WHERE a.student_id=? ORDER BY a.created_at DESC",
                (student_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def for_posting(self, posting_id: str, include_withdrawn: bool = False) -> list[dict]:
        """Applicants for an employer/coordinator. Students are identified by an opaque ref;
        their email is included ONLY under an active `contact` consent. Withdrawn applications
        are excluded by default (B7)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT a.id, a.student_id, a.status, a.human_review_requested, a.created_at, "
                "u.email AS _email, EXISTS(SELECT 1 FROM consents c WHERE c.user_id=a.student_id "
                "AND c.purpose='contact' AND c.revoked_at IS NULL) AS _contact_ok "
                "FROM applications a JOIN users u ON u.id = a.student_id "
                "WHERE a.posting_id=?"
                + ("" if include_withdrawn else " AND a.status != 'withdrawn'")
                + " ORDER BY a.created_at",
                (posting_id,),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["candidate_ref"] = f"student-{row['student_id']}"
            row["email"] = row.pop("_email") if row.pop("_contact_ok") else None
            out.append(row)
        return out

    def set_status(self, app_id: str, to_status: str) -> dict:
        if to_status == "withdrawn":
            raise StudentError("Students withdraw their own applications.")
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise StudentError("No such application.")
            if to_status not in _APP_TRANSITIONS.get(row["status"], set()):
                raise StudentError(f"Can't move a {row['status']} application to {to_status}.")
            conn.execute("UPDATE applications SET status=?, updated_at=? WHERE id=?",
                         (to_status, time.time(), app_id))
            conn.commit()
        return self.get(app_id)

    def withdraw(self, app_id: str, student_id: int) -> dict:
        """B7: student-initiated, ownership-checked, terminal (nothing leaves 'withdrawn')."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT status FROM applications WHERE id=? AND student_id=?",
                               (app_id, student_id)).fetchone()
            if row is None:
                raise StudentError("No such application.")
            if "withdrawn" not in _APP_TRANSITIONS.get(row["status"], set()):
                raise StudentError(f"Can't withdraw a {row['status']} application.")
            conn.execute("UPDATE applications SET status='withdrawn', updated_at=? WHERE id=?",
                         (time.time(), app_id))
            conn.commit()
        return self.get(app_id)

    def request_human_review(self, app_id: str, student_id: int) -> None:
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE applications SET human_review_requested=1, updated_at=? "
                "WHERE id=? AND student_id=?",
                (time.time(), app_id, student_id),
            )
            if not cur.rowcount:
                raise StudentError("No such application.")
            conn.commit()
