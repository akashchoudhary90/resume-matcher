"""Posting + org DAOs over the platform DB (docs/PLATFORM.md Slice F).

PostingStore owns the posting lifecycle state machine — every transition is validated against
_TRANSITIONS and appended to the posting_events log (the compliance graft: an auditor can replay
who moved what, when). OrgStore owns the employer↔school approval links (approval is a LINK row,
never a boolean on the org).

The Ontario Working-for-Workers AI-disclosure block is appended AT APPROVAL — mandatory, not
employer-optional (docs/PLATFORM.md graft #8).
"""
from __future__ import annotations

import json
import secrets
import time
from contextlib import closing

from .db import connect, migrate, platform_db_path

AI_DISCLOSURE = (
    "Disclosure: applications to this posting are managed on a platform that uses artificial "
    "intelligence to help screen and rank candidates (AI-assisted screening). AI output is an "
    "advisory fit/readiness assessment reviewed by humans; no application is auto-rejected."
)

# from-status -> {to-status: action-name}. Anything else is a 409.
_TRANSITIONS: dict[str, dict[str, str]] = {
    "draft": {"pending_review": "submit"},
    "pending_review": {"live": "approve", "rejected": "reject"},
    "rejected": {"pending_review": "submit"},
    "live": {"closed": "close"},
}

# Columns a PATCH may touch (everything else is lifecycle- or system-owned).
_EDITABLE = {"title", "description", "location", "work_mode", "employment_type", "pay_min",
             "pay_max", "pay_currency", "pay_period", "apply_deadline", "start_date",
             "min_education", "min_years", "application_method", "application_url"}


class PostingError(Exception):
    """Client-correctable posting problem -> HTTP 400/409 at the route."""


class PostingStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    # ---- create / read ---------------------------------------------------------------------------
    def create(self, *, created_by: int, org_id: int | None, fields: dict,
               skills: list[dict] | None = None, extraction: dict | None = None,
               school_id: int = 1) -> str:
        title = str(fields.get("title") or "").strip()
        if not title:
            raise PostingError("A posting needs a title.")
        posting_id = secrets.token_urlsafe(10)
        now = time.time()
        cols = {k: fields.get(k) for k in _EDITABLE}
        cols["title"] = title
        cols["description"] = str(fields.get("description") or "")
        with closing(self._conn()) as conn:
            conn.execute(
                f"INSERT INTO postings(id, school_id, org_id, created_by, status, "
                f"{', '.join(cols)}, extraction_json, created_at, updated_at) "
                f"VALUES(?,?,?,?,'draft',{','.join('?' * len(cols))},?,?,?)",
                (posting_id, school_id, org_id, created_by, *cols.values(),
                 json.dumps(extraction) if extraction else None, now, now),
            )
            self._replace_skills(conn, posting_id, skills or [])
            conn.execute(
                "INSERT INTO posting_events(posting_id, actor_user_id, from_status, to_status, "
                "note, at) VALUES(?,?,NULL,'draft','created',?)",
                (posting_id, created_by, now),
            )
            conn.commit()
        return posting_id

    @staticmethod
    def _replace_skills(conn, posting_id: str, skills: list[dict]) -> None:
        conn.execute("DELETE FROM posting_skills WHERE posting_id=?", (posting_id,))
        seen: set[str] = set()
        for s in skills:
            sid = str(s.get("skill_id") or "").strip()
            bucket = s.get("bucket") if s.get("bucket") in ("must_have", "required", "preferred") \
                else "required"
            if not sid or sid in seen:
                continue
            seen.add(sid)
            conn.execute(
                "INSERT INTO posting_skills(posting_id, skill_id, bucket, source) VALUES(?,?,?,?)",
                (posting_id, sid, bucket, str(s.get("source") or "user")),
            )

    def get(self, posting_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
            if row is None:
                return None
            posting = dict(row)
            posting["skills"] = [dict(r) for r in conn.execute(
                "SELECT skill_id, bucket, source FROM posting_skills WHERE posting_id=? "
                "ORDER BY bucket, skill_id", (posting_id,))]
            posting["org_name"] = None
            if posting["org_id"]:
                org = conn.execute("SELECT name FROM orgs WHERE id=?",
                                   (posting["org_id"],)).fetchone()
                posting["org_name"] = org["name"] if org else None
        if posting.get("extraction_json"):
            posting["extraction"] = json.loads(posting.pop("extraction_json"))
        else:
            posting.pop("extraction_json", None)
            posting["extraction"] = None
        return posting

    def list(self, *, status: str | None = None, org_id: int | None = None,
             school_id: int = 1) -> list[dict]:
        where, params = ["school_id=?"], [school_id]
        if status:
            where.append("status=?")
            params.append(status)
        if org_id is not None:
            where.append("org_id=?")
            params.append(org_id)
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT p.id, p.title, p.status, p.org_id, o.name AS org_name, p.location, "
                "p.work_mode, p.employment_type, p.pay_min, p.pay_max, p.pay_currency, "
                "p.pay_period, p.apply_deadline, p.created_at, p.updated_at "
                "FROM postings p LEFT JOIN orgs o ON o.id = p.org_id "
                f"WHERE {' AND '.join(where)} ORDER BY p.updated_at DESC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- update / lifecycle ----------------------------------------------------------------------
    def update_fields(self, posting_id: str, fields: dict,
                      skills: list[dict] | None = None) -> None:
        cols = {k: v for k, v in fields.items() if k in _EDITABLE}
        with closing(self._conn()) as conn:
            if cols:
                sets = ", ".join(f"{k}=?" for k in cols)
                conn.execute(f"UPDATE postings SET {sets}, updated_at=? WHERE id=?",
                             (*cols.values(), time.time(), posting_id))
            if skills is not None:
                self._replace_skills(conn, posting_id, skills)
            conn.commit()

    def transition(self, posting_id: str, to_status: str, *, actor_user_id: int,
                   note: str = "") -> dict:
        """Validated state-machine move + append-only event. Approval stamps the reviewer and
        appends the (non-optional) AI-disclosure block."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT status, description FROM postings WHERE id=?",
                               (posting_id,)).fetchone()
            if row is None:
                raise PostingError("No such posting.")
            from_status = row["status"]
            action = _TRANSITIONS.get(from_status, {}).get(to_status)
            if action is None:
                raise PostingError(f"Can't move a {from_status} posting to {to_status}.")
            now = time.time()
            if action == "approve":
                description = row["description"] or ""
                if AI_DISCLOSURE not in description:
                    description = f"{description.rstrip()}\n\n{AI_DISCLOSURE}"
                conn.execute(
                    "UPDATE postings SET status=?, description=?, ai_disclosure=1, reviewed_by=?, "
                    "reviewed_at=?, updated_at=? WHERE id=?",
                    (to_status, description, actor_user_id, now, now, posting_id))
            elif action == "reject":
                conn.execute(
                    "UPDATE postings SET status=?, reviewed_by=?, reviewed_at=?, updated_at=? "
                    "WHERE id=?", (to_status, actor_user_id, now, now, posting_id))
            else:
                conn.execute("UPDATE postings SET status=?, updated_at=? WHERE id=?",
                             (to_status, now, posting_id))
            conn.execute(
                "INSERT INTO posting_events(posting_id, actor_user_id, from_status, to_status, "
                "note, at) VALUES(?,?,?,?,?,?)",
                (posting_id, actor_user_id, from_status, to_status, note or action, now))
            conn.commit()
        return self.get(posting_id)

    def events(self, posting_id: str) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT actor_user_id, from_status, to_status, note, at FROM posting_events "
                "WHERE posting_id=? ORDER BY at, id", (posting_id,)).fetchall()
        return [dict(r) for r in rows]


class OrgStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def link_status(self, org_id: int, school_id: int = 1) -> str | None:
        with closing(connect(self.path)) as conn:
            row = conn.execute(
                "SELECT status FROM employer_school_links WHERE org_id=? AND school_id=?",
                (org_id, school_id)).fetchone()
        return row["status"] if row else None

    def set_link_status(self, org_id: int, status: str, *, reviewed_by: int,
                        school_id: int = 1) -> None:
        if status not in ("approved", "revoked"):
            raise PostingError("Link status must be approved or revoked.")
        with closing(connect(self.path)) as conn:
            cur = conn.execute(
                "UPDATE employer_school_links SET status=?, reviewed_by=?, reviewed_at=? "
                "WHERE org_id=? AND school_id=?",
                (status, reviewed_by, time.time(), org_id, school_id))
            if not cur.rowcount:
                raise PostingError("No such employer link.")
            conn.commit()

    def pending_links(self, school_id: int = 1) -> list[dict]:
        with closing(connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT l.org_id, o.name AS org_name, l.created_at FROM employer_school_links l "
                "JOIN orgs o ON o.id = l.org_id WHERE l.school_id=? AND l.status='pending' "
                "ORDER BY l.created_at", (school_id,)).fetchall()
        return [dict(r) for r in rows]
