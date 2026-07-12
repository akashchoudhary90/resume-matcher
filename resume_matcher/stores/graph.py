"""Relationship-graph network store (docs/RELATIONSHIPS.md Slices AB/Z).

Two jobs:
  * register_identity — when a member opts into `graph_discoverable`, they tell us the name+company
    (and/or email) their connections would know them by; we store ONLY the opaque per-school
    token(s), never the cleartext, so others' uploaded contact lists can be intersected against
    them without us holding anyone's name.
  * import_csv — a member uploads their OWN LinkedIn Connections.csv. We parse it in RAM, tokenize
    each row, INTERSECT against consenting members' tokens, and keep ONLY edges to matched members.
    Every non-matching contact is discarded before any write — zero server-side residue for
    non-members (the load-bearing privacy decision; boundary #1).

Erasure + suppression helpers back the data-subject-request path (Slice Z).
"""
from __future__ import annotations

import csv
import secrets
import time
from contextlib import closing

from . import graph_tokens
from .db import connect, migrate, platform_db_path

MAX_IMPORT_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_IMPORT_ROWS = 30_000
MIN_IMPORT_ROWS = 3                  # anti-oracle: reject 1-2 row "probe" uploads


class GraphError(Exception):
    """Client-correctable problem -> HTTP 400/409 at the route."""


class NetworkStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    # ---- discovery identity (tokens only; no cleartext) ------------------------------------------
    def register_identity(self, user_id: int, school_id: int, *, first: str = "", last: str = "",
                          company: str = "", email: str = "") -> int:
        """Register the member's discoverable identity token(s). Stores tokens only. Returns how
        many tokens were registered (0 if unidentifiable)."""
        tokens: list[tuple[str, str]] = []
        by_email = graph_tokens.identity_token(school_id, email=email) if email else None
        by_name = graph_tokens.identity_token(school_id, first=first, last=last, company=company)
        for t in (by_email, by_name):
            if t is not None:
                tokens.append(t)
        if not tokens:
            return 0
        now = time.time()
        with closing(self._conn()) as conn:
            for token, kv in tokens:
                conn.execute(
                    "INSERT OR IGNORE INTO member_graph_identity(user_id, school_id, "
                    "identity_token, key_version, created_at) VALUES(?,?,?,?,?)",
                    (user_id, school_id, token, kv, now))
            conn.commit()
        return len(tokens)

    def clear_identity(self, user_id: int) -> None:
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM member_graph_identity WHERE user_id=?", (user_id,))
            conn.commit()

    # ---- PSI-lite import (RAM-only intersection; zero non-member residue) ------------------------
    def import_csv(self, user_id: int, school_id: int, raw: bytes) -> dict:
        """Parse the member's own export in RAM, tokenize rows, intersect against consenting
        members, and create pending edges only to matches. Returns a job-internal summary; the
        ROUTE must NOT surface per-contact counts to the uploader (membership-oracle fix)."""
        if not graph_tokens.available():
            raise GraphError("The contacts importer is not configured on this deployment.")
        if len(raw) > MAX_IMPORT_BYTES:
            raise GraphError("That file is too large (5 MB max).")
        rows = _parse_linkedin_csv(raw)
        if len(rows) < MIN_IMPORT_ROWS:
            raise GraphError("That export has too few contacts to import.")
        if len(rows) > MAX_IMPORT_ROWS:
            rows = rows[:MAX_IMPORT_ROWS]

        # tokenize every contact IN RAM
        contact_tokens: set[str] = set()
        for r in rows:
            t = graph_tokens.identity_token(
                school_id, first=r.get("first", ""), last=r.get("last", ""),
                company=r.get("company", ""), email=r.get("email", ""))
            if t is not None:
                contact_tokens.add(t[0])
        if not contact_tokens:
            return {"rows_seen": len(rows), "edges_created": 0}

        # intersect against consenting discoverable members (RAM); look up who each hit belongs to
        with closing(self._conn()) as conn:
            marks = ",".join("?" * len(contact_tokens))
            hits = conn.execute(
                f"SELECT DISTINCT mgi.user_id AS uid FROM member_graph_identity mgi "
                f"WHERE mgi.school_id=? AND mgi.identity_token IN ({marks}) "
                f"AND mgi.user_id != ? "
                f"AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=mgi.user_id "
                f"          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                f"AND NOT EXISTS(SELECT 1 FROM graph_suppressions s "
                f"          WHERE s.user_id=mgi.user_id OR s.identity_token=mgi.identity_token)",
                (school_id, *contact_tokens, user_id),
            ).fetchall()
            matched_user_ids = [row["uid"] for row in hits]
            # create pending self_upload edges to matches (deferred to RelationshipStore semantics,
            # but written here so the whole intersection stays in one transaction / one RAM pass)
            created = 0
            now = time.time()
            for other in matched_user_ids:
                a, b = sorted((user_id, other))
                edge_key = f"{a}\x1f{b}\x1flinkedin_connection"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO graph_edges(id, school_id, edge_key, user_a, user_b, "
                    "kind, weight, last_seen_at, provenance, consent_state, owner_user_id, "
                    "created_at, updated_at, expires_at) "
                    "VALUES(?,?,?,?,?,'linkedin_connection',0.30,?,'self_upload','pending',?,?,?,?)",
                    (secrets.token_urlsafe(10), school_id, edge_key, a, b, now, user_id,
                     now, now, now + 365 * 86400),
                )
                created += cur.rowcount
            conn.commit()
        # contact_tokens (incl. all non-matches) fall out of scope here — never persisted.
        return {"rows_seen": len(rows), "edges_created": created}

    # ---- erasure / suppression (Slice Z) ---------------------------------------------------------
    def delete_my_network(self, user_id: int) -> dict:
        """Hard-delete everything graph-related about a member + tombstone so nothing re-materializes."""
        now = time.time()
        with closing(self._conn()) as conn:
            school = conn.execute("SELECT school_id FROM users WHERE id=?",
                                  (user_id,)).fetchone()
            school_id = school["school_id"] if school else 1
            conn.execute("DELETE FROM graph_edges WHERE user_a=? OR user_b=?", (user_id, user_id))
            conn.execute("DELETE FROM member_graph_identity WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, user_id, reason, created_at) "
                "VALUES(?,?,'member_optout',?)", (school_id, user_id, now))
            conn.commit()
        return {"ok": True}

    def repudiate(self, school_id: int, *, first: str = "", last: str = "", company: str = "",
                  email: str = "") -> dict:
        """Non-member data-subject request: tokenize the self-asserted identity in RAM, tombstone
        the token, and hard-delete any edge/identity that resolved to it. The person never had an
        account and we never stored their name — the token is the only handle."""
        t = graph_tokens.identity_token(school_id, first=first, last=last, company=company,
                                        email=email)
        if t is None:
            raise GraphError("Not enough information to identify a record to remove.")
        token = t[0]
        now = time.time()
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, identity_token, reason, created_at) "
                "VALUES(?,?,'third_party_repudiation',?)", (school_id, token, now))
            # remove any member identity that happens to match (and their derived edges)
            uids = [r["user_id"] for r in conn.execute(
                "SELECT user_id FROM member_graph_identity WHERE school_id=? AND identity_token=?",
                (school_id, token))]
            for uid in uids:
                conn.execute("DELETE FROM graph_edges WHERE user_a=? OR user_b=?", (uid, uid))
            conn.execute("DELETE FROM member_graph_identity WHERE school_id=? AND identity_token=?",
                         (school_id, token))
            conn.commit()
        return {"ok": True, "suppressed": True}


def _parse_linkedin_csv(raw: bytes) -> list[dict]:
    """Parse a LinkedIn Connections.csv from RAM. LinkedIn prepends a few 'Notes:' preamble lines
    before the real header row (First Name,Last Name,...). Tolerant of encoding + column variance."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()
    # find the header line (the first line containing 'First Name')
    start = 0
    for i, line in enumerate(lines[:10]):
        if "first name" in line.lower():
            start = i
            break
    reader = csv.DictReader(lines[start:])
    out: list[dict] = []
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        first = norm.get("first name", "")
        last = norm.get("last name", "")
        company = norm.get("company", "")
        email = norm.get("email address", "")
        if first or last or email:
            out.append({"first": first, "last": last, "company": company, "email": email})
    return out
