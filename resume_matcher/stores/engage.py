"""Engagement stores (docs/IMPLEMENTATION.md Phase 3, slices R/S/T): career-fair events,
application-thread messaging, and interview scheduling.

Design stances:
  * Events are school-scoped and coordinator-owned; students RSVP, employers book a booth.
  * Messaging exists ONLY on an application thread — the applicant, the posting org's employers,
    and coordinators. There is deliberately no cold-outreach channel (anti-spam + privacy).
  * Interviews: an employer proposes 1..N slots on an application; the student accepts exactly
    one (siblings auto-decline); either side can cancel.
"""
from __future__ import annotations

import secrets
import time
from contextlib import closing

from .db import connect, migrate, platform_db_path


class EngageError(Exception):
    """Client-correctable problem -> HTTP 400/409 at the route."""


class _Store:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)


# ---- Slice R: events -----------------------------------------------------------------------------
class EventStore(_Store):
    def create(self, *, created_by: int, school_id: int, title: str, kind: str = "fair",
               description: str = "", location: str = "", starts_at: float = 0.0,
               ends_at: float | None = None) -> str:
        title = (title or "").strip()
        if not title:
            raise EngageError("An event needs a title.")
        if kind not in ("fair", "info_session", "workshop"):
            raise EngageError(f"Unknown event kind {kind!r}.")
        if not starts_at:
            raise EngageError("An event needs a start time.")
        event_id = secrets.token_urlsafe(10)
        now = time.time()
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO campus_events(id, school_id, kind, title, description, location, starts_at,"
                " ends_at, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, school_id, kind, title[:200], description, location, starts_at,
                 ends_at, created_by, now, now),
            )
            conn.commit()
        return event_id

    def set_status(self, event_id: str, status: str) -> dict:
        if status not in ("published", "cancelled"):
            raise EngageError("Events can only be published or cancelled.")
        with closing(self._conn()) as conn:
            cur = conn.execute("UPDATE campus_events SET status=?, updated_at=? WHERE id=?",
                               (status, time.time(), event_id))
            if not cur.rowcount:
                raise EngageError("No such event.")
            conn.commit()
        return self.get(event_id)

    def get(self, event_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM campus_events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list(self, *, school_id: int, include_drafts: bool = False) -> list[dict]:
        where = "school_id=?" + ("" if include_drafts else " AND status='published'")
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"SELECT e.*, (SELECT COUNT(*) FROM event_registrations r WHERE r.event_id=e.id "
                f"AND r.status='registered') AS registrations FROM campus_events e WHERE {where} "
                "ORDER BY starts_at", (school_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def register(self, event_id: str, user_id: int, role: str) -> None:
        event = self.get(event_id)
        if event is None or event["status"] != "published":
            raise EngageError("That event isn't open for registration.")
        with closing(self._conn()) as conn:
            existing = conn.execute(
                "SELECT status FROM event_registrations WHERE event_id=? AND user_id=?",
                (event_id, user_id)).fetchone()
            if existing and existing["status"] == "registered":
                raise EngageError("You're already registered for this event.")
            conn.execute(
                "INSERT INTO event_registrations(event_id, user_id, role, created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(event_id, user_id) DO UPDATE SET "
                "status='registered', created_at=excluded.created_at",
                (event_id, user_id, role, time.time()),
            )
            conn.commit()

    def unregister(self, event_id: str, user_id: int) -> None:
        with closing(self._conn()) as conn:
            conn.execute("UPDATE event_registrations SET status='cancelled' "
                         "WHERE event_id=? AND user_id=?", (event_id, user_id))
            conn.commit()

    def attendees(self, event_id: str) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT r.user_id, r.role, r.created_at, u.email, o.name AS org_name "
                "FROM event_registrations r JOIN users u ON u.id = r.user_id "
                "LEFT JOIN orgs o ON o.id = u.org_id "
                "WHERE r.event_id=? AND r.status='registered' ORDER BY r.created_at",
                (event_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def my_registrations(self, user_id: int) -> set[str]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT event_id FROM event_registrations WHERE user_id=? "
                                "AND status='registered'", (user_id,)).fetchall()
        return {r["event_id"] for r in rows}


# ---- Slice S: application-thread messaging ---------------------------------------------------------
class MessageStore(_Store):
    def send(self, application_id: str, sender_user_id: int, body: str) -> dict:
        body = (body or "").strip()
        if not body:
            raise EngageError("Empty message.")
        if len(body) > 4000:
            raise EngageError("Message too long (4000 chars max).")
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "INSERT INTO messages(application_id, sender_user_id, body, sent_at) "
                "VALUES(?,?,?,?)",
                (application_id, sender_user_id, body, time.time()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def thread(self, application_id: str, reader_user_id: int) -> list[dict]:
        """The thread, marking everything not sent by the reader as read."""
        with closing(self._conn()) as conn:
            conn.execute(
                "UPDATE messages SET read_at=? WHERE application_id=? AND sender_user_id != ? "
                "AND read_at IS NULL",
                (time.time(), application_id, reader_user_id),
            )
            rows = conn.execute(
                "SELECT m.*, u.role AS sender_role FROM messages m "
                "JOIN users u ON u.id = m.sender_user_id "
                "WHERE m.application_id=? ORDER BY m.sent_at, m.id",
                (application_id,),
            ).fetchall()
            conn.commit()
        return [dict(r) for r in rows]

    def unread_count(self, user_id: int, application_ids: list[str]) -> int:
        if not application_ids:
            return 0
        marks = ",".join("?" * len(application_ids))
        with closing(self._conn()) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE application_id IN ({marks}) "
                "AND sender_user_id != ? AND read_at IS NULL",
                (*application_ids, user_id),
            ).fetchone()[0]


# ---- Slice T: interview scheduling ------------------------------------------------------------------
class InterviewStore(_Store):
    def propose(self, application_id: str, proposed_by: int,
                slots: list[dict]) -> list[dict]:
        if not slots:
            raise EngageError("Propose at least one time slot.")
        now = time.time()
        created = []
        with closing(self._conn()) as conn:
            for s in slots[:10]:
                starts, ends = float(s.get("starts_at") or 0), float(s.get("ends_at") or 0)
                if not starts or ends <= starts:
                    raise EngageError("Each slot needs starts_at < ends_at (unix seconds).")
                slot_id = secrets.token_urlsafe(8)
                conn.execute(
                    "INSERT INTO interview_slots(id, application_id, proposed_by, starts_at, "
                    "ends_at, created_at) VALUES(?,?,?,?,?,?)",
                    (slot_id, application_id, proposed_by, starts, ends, now),
                )
                created.append(slot_id)
            conn.commit()
        return [self.get(sid) for sid in created]

    def get(self, slot_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM interview_slots WHERE id=?", (slot_id,)).fetchone()
        return dict(row) if row else None

    def for_application(self, application_id: str) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT * FROM interview_slots WHERE application_id=? ORDER BY starts_at",
                (application_id,)).fetchall()
        return [dict(r) for r in rows]

    def accept(self, slot_id: str) -> dict:
        """Accept ONE slot; every sibling proposal on the application auto-declines."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT application_id, status FROM interview_slots WHERE id=?",
                               (slot_id,)).fetchone()
            if row is None:
                raise EngageError("No such slot.")
            if row["status"] != "proposed":
                raise EngageError(f"That slot is already {row['status']}.")
            conn.execute("UPDATE interview_slots SET status='accepted' WHERE id=?", (slot_id,))
            conn.execute(
                "UPDATE interview_slots SET status='declined' WHERE application_id=? "
                "AND id != ? AND status='proposed'",
                (row["application_id"], slot_id),
            )
            conn.commit()
        return self.get(slot_id)

    def cancel(self, slot_id: str) -> dict:
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE interview_slots SET status='cancelled' WHERE id=? "
                "AND status IN ('proposed','accepted')", (slot_id,))
            if not cur.rowcount:
                raise EngageError("That slot can't be cancelled.")
            conn.commit()
        return self.get(slot_id)

    def upcoming_for_student(self, student_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT s.*, a.posting_id, p.title, o.name AS org_name "
                "FROM interview_slots s JOIN applications a ON a.id = s.application_id "
                "JOIN postings p ON p.id = a.posting_id LEFT JOIN orgs o ON o.id = p.org_id "
                "WHERE a.student_id=? AND s.status IN ('proposed','accepted') "
                "ORDER BY s.starts_at",
                (student_id,)).fetchall()
        return [dict(r) for r in rows]
