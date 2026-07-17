"""In-app notifications (Phase 5, B4-lite — docs/PHASE5.md §2.11).

Design stances:
  * NO user free text is ever stored: title/body are server-composed; entity/entity_id point at
    the source object for deep links. Composed text must never embed a user email — otherwise
    account erasure (A3) would leave the erased user's address inside OTHER users' notification
    rows. notify() enforces that invariant at the chokepoint (erasure hygiene, FL-L4).
  * Every read/update carries WHERE user_id=? (security L1): a supplied ids list can never touch
    another user's rows.
  * The silent-decline invariant (D8) lives at the fan-out call sites, not here: intro declines
    and mentorship declines simply never call notify().
  * The optional email_to fan-out rides notify.send()'s existing best-effort contract (silent
    no-op when RM_SMTP_HOST is unset); rows are retention-purged by run_retention() (§2.15).
"""
from __future__ import annotations

import re
import time
from contextlib import closing

from .. import notify as email_notify
from .db import connect, migrate, platform_db_path

# Mirrors the CHECK in migrations/004_phase5.sql (no drift — a new kind lands in both places).
NOTIFICATION_KINDS = (
    "message", "interview_proposed", "interview_cancelled", "intro_request", "intro_accepted",
    "vouch_received", "application_status", "posting_approved", "posting_rejected",
    "mentorship_offer", "mentorship_accepted", "affiliation_confirmed", "bridge_created",
    "vouch_contested", "repudiation_notice",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class NotificationStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    def notify(self, user_id: int, school_id: int, kind: str, title: str,
               body: str = "", entity: str | None = None, entity_id: str | None = None,
               *, email_to: str | None = None) -> int:
        if kind not in NOTIFICATION_KINDS:
            raise ValueError(f"Unknown notification kind {kind!r}.")
        if _EMAIL_RE.search(title) or _EMAIL_RE.search(body):
            raise ValueError("Notification titles/bodies must never embed an email address "
                             "(erasure hygiene — compose from org/posting content instead).")
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "INSERT INTO notifications(user_id, school_id, kind, title, body, entity, "
                "entity_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (user_id, school_id, kind, title, body, entity, entity_id, time.time()),
            )
            conn.commit()
            row_id = cur.lastrowid
        if email_to:  # best-effort by contract: a mail failure never fails the action
            email_notify.send(email_to, title, body)
        return row_id

    def feed(self, user_id: int, *, unread_only: bool = False,
             page: int = 1, page_size: int = 20) -> dict:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        where = "WHERE user_id=?" + (" AND read_at IS NULL" if unread_only else "")
        with closing(self._conn()) as conn:
            items = conn.execute(
                f"SELECT id, kind, title, body, entity, entity_id, created_at, read_at "
                f"FROM notifications {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, page_size, (page - 1) * page_size),
            ).fetchall()
            unread = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                (user_id,),
            ).fetchone()[0]
        return {"items": [dict(r) for r in items], "unread": unread, "page": page}

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int:
        """None = mark ALL of this user's unread rows; the WHERE user_id=? makes a supplied ids
        list harmless against other users' rows (security L1)."""
        now = time.time()
        with closing(self._conn()) as conn:
            if ids is None:
                cur = conn.execute(
                    "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                    (now, user_id),
                )
            else:
                if not ids:
                    return 0
                placeholders = ",".join("?" * len(ids))
                cur = conn.execute(
                    f"UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL "
                    f"AND id IN ({placeholders})",
                    (now, user_id, *[int(i) for i in ids]),
                )
            conn.commit()
            return cur.rowcount

    def purge(self, *, read_older_than_s: float = 90 * 86400,
              unread_older_than_s: float = 180 * 86400) -> int:
        """Retention window (called by run_retention): read rows after 90d, unread after 180d."""
        now = time.time()
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "DELETE FROM notifications WHERE (read_at IS NOT NULL AND created_at < ?) "
                "OR (read_at IS NULL AND created_at < ?)",
                (now - read_older_than_s, now - unread_older_than_s),
            )
            conn.commit()
            return cur.rowcount
