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

Phase-5 (A1, docs/PHASE5.md §2.2) replaces the old public instant `repudiate()` with a QUEUE and
two narrowly-scoped executors, because an unauthenticated third-party assertion must never be a
delete button on someone else's graph:
  * `repudiate_execute_email` — reachable ONLY from `confirm_repudiation`, i.e. after the
    requester proved control of the address. Proving the address makes the deletion self-action,
    so it may touch member data — but only SELF-UPLOAD-derived edges, never native activity.
  * `repudiate_execute_name` — reachable ONLY from `decide_repudiation`, i.e. after a coordinator
    approved. It NEVER touches an active member (privacy F3): a name assertion is not proof, so a
    matched member keeps every row and instead gets a `repudiation_notice` pointing at their own
    self-serve controls. For non-members it suppresses the token and deletes matching
    employer-contact rows (privacy F5).
No other caller may reach an executor (security L2) — the challenge/queue IS the authorization.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import closing

from ..inference.redaction import redact_text
from . import graph_tokens
from .db import connect, migrate, platform_db_path
from .notifications import NotificationStore

_log = logging.getLogger("resume_matcher.stores.graph")

MAX_IMPORT_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_IMPORT_ROWS = 30_000
MIN_IMPORT_ROWS = 3                  # anti-oracle: reject 1-2 row "probe" uploads

_MAX_ASSERTED = 80                   # length cap on every asserted third-party field (security H1)
_CHALLENGE_TTL_S = 48 * 3600         # an email challenge dies in 48h
_PURGE_AFTER_S = 30 * 86400          # a decided/expired DSR record is hard-deleted 30d later
_MAX_CONTACT_SCAN = 20_000           # backstop on the tokenizing employer_contacts scan
_EMPTY_BUCKET: dict = {"contact_ids": [], "member_uids": []}   # read-only miss result


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_asserted(value: str) -> str:
    """Every asserted third-party field is length-capped AND redact_text()'d AT INGEST: this text
    is written by an anonymous public caller and later rendered in a coordinator's queue, so the
    stored-XSS/PII payload must never reach the DB in the first place (security H1). The
    coordinator card escapes it again — defence in depth, not a substitute."""
    return redact_text((value or "").strip()[:_MAX_ASSERTED])


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
    def delete_my_network(self, user_id: int, *, reason: str = "member_optout") -> dict:
        """Hard-delete EVERYTHING graph-related about a member (edges, discovery tokens, vouches as
        voucher and as subject, intro requests in any role) + tombstone so nothing re-materializes.
        This is the erasure cascade (Slice AK); a true DELETE, never a soft flag."""
        now = time.time()
        with closing(self._conn()) as conn:
            school = conn.execute("SELECT school_id FROM users WHERE id=?",
                                  (user_id,)).fetchone()
            school_id = school["school_id"] if school else 1
            conn.execute("DELETE FROM graph_edges WHERE user_a=? OR user_b=?", (user_id, user_id))
            conn.execute("DELETE FROM member_graph_identity WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM vouches WHERE voucher_user_id=? OR subject_user_id=?",
                         (user_id, user_id))
            conn.execute("DELETE FROM intro_requests WHERE requester_user_id=? OR target_user_id=? "
                         "OR broker_user_id=?", (user_id, user_id, user_id))
            # A17: broker blocks are graph-related rows about this member in BOTH directions —
            # the docstring's "EVERYTHING graph-related" was not true until this line existed.
            conn.execute("DELETE FROM broker_blocks WHERE broker_user_id=? OR blocked_user_id=?",
                         (user_id, user_id))
            # intro_events keep only opaque ids + status (no PII), so they need no scrub
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, user_id, reason, created_at) "
                "VALUES(?,?,?,?)", (school_id, user_id, reason, now))
            conn.commit()
        return {"ok": True}

    # ---- A1: repudiation executors (NEVER called from a route — see the module docstring) --------
    def repudiate_execute_email(self, school_id: int, *, email: str) -> dict:
        """EMAIL PATH. Caller: confirm_repudiation only — the requester PROVED control of this
        address, which makes the deletion self-action rather than a stranger's assertion.

        Even so it is narrow: only `self_upload`-derived edges (the ones OTHER members' contact
        imports minted about this identity) die. Native activity edges — applications, interviews,
        message threads — are records of things the platform itself observed and are NOT deleted
        by an email challenge; a member who wants those gone uses account erasure (A3)."""
        t = graph_tokens.identity_token(school_id, email=email)
        if t is None:
            raise GraphError("Not enough information to identify a record to remove.")
        token = t[0]
        now = time.time()
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, identity_token, reason, created_at) "
                "VALUES(?,?,'third_party_repudiation',?)", (school_id, token, now))
            uids = [r["user_id"] for r in conn.execute(
                "SELECT user_id FROM member_graph_identity WHERE school_id=? AND identity_token=?",
                (school_id, token))]
            edges = 0
            for uid in uids:
                edges += conn.execute(
                    "DELETE FROM graph_edges WHERE provenance='self_upload' "
                    "AND (user_a=? OR user_b=?)", (uid, uid)).rowcount
            # only the tokens that resolve to THIS address — a member's other identity rows
            # (e.g. their name+company token) are not in scope of an email challenge
            conn.execute("DELETE FROM member_graph_identity WHERE school_id=? AND identity_token=?",
                         (school_id, token))
            conn.commit()
        return {"ok": True, "suppressed": True, "edges_deleted": edges,
                "members_matched": len(uids)}

    def repudiate_execute_name(self, school_id: int, *, first: str, last: str,
                               company: str) -> dict:
        """NAME PATH. Caller: decide_repudiation on coordinator approval only.

        privacy F3 — a name+company assertion is NOT proof of identity, so this path NEVER touches
        an active member: if the token resolves to one, their rows and edges are left completely
        alone, no member-scoped suppression is written (that would tombstone them out of the graph
        on a stranger's say-so), and they get a `repudiation_notice` pointing at their own
        self-serve controls. Only genuinely non-member identities are suppressed + their derived
        employer-contact rows deleted (privacy F5).

        "Active member" is NOT the same question as "has a member_graph_identity row": mgi is
        written only by register_identity, i.e. only for members who opted into graph_discoverable.
        A hiring manager who never opted in has zero mgi rows, so an mgi-only test called them a
        stranger and hard-deleted their live employer_contacts rows on a name match. Membership is
        therefore tested against employer_contacts.contact_user_id as well (see _contact_index)."""
        t = graph_tokens.identity_token(school_id, first=first, last=last, company=company)
        if t is None:
            raise GraphError("Not enough information to identify a record to remove.")
        token = t[0]
        now = time.time()
        with closing(self._conn()) as conn:
            members = [dict(r) for r in conn.execute(
                "SELECT mgi.user_id AS uid, u.school_id AS school_id FROM member_graph_identity mgi "
                "JOIN users u ON u.id=mgi.user_id "
                "WHERE mgi.school_id=? AND mgi.identity_token=?", (school_id, token))]
            bucket = self._contact_index(conn, school_id).get(token, _EMPTY_BUCKET)
            # both membership routes, deduped: the mgi rows and the contact rows that name a live user
            targets = {m["uid"]: m["school_id"] or school_id for m in members}
            for uid in bucket["member_uids"]:
                targets.setdefault(uid, school_id)
            if targets:
                # refuse: no suppression row, no deletion — just tell the member it happened.
                # Refusal is whole-request, not per-row: a token that collides with a member is
                # ambiguous evidence, and suppressing it would tombstone the member's identity too.
                conn.commit()
                for uid, sid in targets.items():
                    self._notify_repudiation_target(uid, sid)
                return {"member_matched": True, "contacts_deleted": 0}
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, identity_token, reason, created_at) "
                "VALUES(?,?,'third_party_repudiation',?)", (school_id, token, now))
            contact_ids = bucket["contact_ids"]
            for cid in contact_ids:
                conn.execute("DELETE FROM posting_contacts WHERE employer_contact_id=?", (cid,))
                conn.execute("DELETE FROM employer_contacts WHERE id=?", (cid,))
            conn.commit()
        return {"member_matched": False, "contacts_deleted": len(contact_ids)}

    @staticmethod
    def _notify_repudiation_target(user_id: int, school_id: int) -> None:
        """Best-effort, like every other fan-out: a mail/notify failure must not fail the DSR."""
        try:
            NotificationStore().notify(
                user_id, school_id, "repudiation_notice",
                "Someone asked us to remove a person matching your details",
                "We did not change anything about your account — a name match is not proof of "
                "identity. If you want your relationship graph removed, use the network controls "
                "on your profile, or delete your account.",
                entity="user", entity_id=str(user_id))
        except Exception:  # pragma: no cover - notification is advisory
            pass

    @staticmethod
    def _contact_index(conn, school_id: int) -> dict[str, dict]:
        """ONE tokenizing pass over a school's employer_contacts ->
        {token: {"contact_ids": [...], "member_uids": [...]}}.

        display_label is free text (a business contact under the PIPEDA exemption), so it is
        tokenized the same way an imported contact row is: name parts + the org as company.

        The two buckets ARE the F3 invariant. A row whose contact_user_id resolves to a live users
        row is an active member's record: it lands in member_uids and NEVER in contact_ids, so the
        name path can neither delete it nor mistake its owner for a stranger. Rows naming a
        genuine non-member (contact_user_id NULL, or a user id whose account was erased) are the
        only deletable ones (privacy F5).

        One pass, indexed by token, rather than one scan per token: list_repudiations previews EVERY
        pending row, and a per-row scan made the coordinator's queue O(pending x contacts) HMACs on
        a queue an anonymous caller can grow. The token equality is a dict lookup rather than
        hmac.compare_digest because both sides are server-derived from the same pepper — there is no
        caller-supplied secret here to leak by timing. LIMIT is a last-resort backstop."""
        out: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT ec.id AS id, ec.display_label AS label, o.name AS org_name, "
            "u.id AS member_uid FROM employer_contacts ec "
            "LEFT JOIN orgs o ON o.id=ec.org_id "
            "LEFT JOIN users u ON u.id=ec.contact_user_id "
            "WHERE ec.school_id=? AND ec.display_label IS NOT NULL LIMIT ?",
            (school_id, _MAX_CONTACT_SCAN)):
            parts = [p for p in (r["label"] or "").split() if p]
            if len(parts) < 2:
                continue
            t = graph_tokens.identity_token(school_id, first=parts[0], last=parts[-1],
                                            company=r["org_name"] or "")
            if t is None:
                continue
            bucket = out.setdefault(t[0], {"contact_ids": [], "member_uids": []})
            if r["member_uid"] is not None:
                bucket["member_uids"].append(r["member_uid"])
            else:
                bucket["contact_ids"].append(r["id"])
        return out

    # ---- A1: the public queue (the ONLY way in) --------------------------------------------------
    def create_repudiation(self, school_id: int, *, kind: str, email: str = "", first: str = "",
                           last: str = "", company: str = "") -> dict:
        """Queue a third-party removal request. Returns {"request_id", "email_token"}; the token is
        for the mailer ONLY — its sha256 is all that persists, and it is never returned to the HTTP
        caller (that would hand the challenge to whoever guessed the address).

        Anti-bombing (security H3): the per-email and global 24h send caps live HERE, not at the
        route's IP limiter, because `_client_key` trusts an X-Forwarded-For hop and is therefore
        spoofable by anyone not behind the trusted front. A capped request returns the SAME shape
        with no token, so a caller can't tell capping from sending (no oracle)."""
        if kind not in ("email_challenge", "name_review"):
            raise GraphError("Unknown repudiation kind.")
        request_id = secrets.token_urlsafe(10)
        now = time.time()
        if kind == "email_challenge":
            email = (email or "").strip()
            if not email:
                raise GraphError("Not enough information to identify a record to remove.")
            email_hash = _sha256(email.lower())
            with closing(self._conn()) as conn:
                per_email = conn.execute(
                    "SELECT COUNT(*) FROM repudiation_requests WHERE kind='email_challenge' "
                    "AND email_hash=? AND created_at > ?",
                    (email_hash, now - 86400)).fetchone()[0]
                # school-scoped: an unscoped global count let one school's traffic silently deny
                # every other school's non-member DSR right, which is a legal right — a tenant must
                # not be able to exhaust another tenant's. The cap is still IP-independent.
                global_24h = conn.execute(
                    "SELECT COUNT(*) FROM repudiation_requests WHERE kind='email_challenge' "
                    "AND school_id=? AND created_at > ?", (school_id, now - 86400)).fetchone()[0]
                capped_email = per_email >= _env_int("RM_REPUDIATE_MAX_PER_EMAIL", 3)
                capped_global = global_24h >= _env_int("RM_REPUDIATE_MAX_EMAILS_PER_DAY", 50)
                if capped_email or capped_global:
                    # a swallowed DSR is the bad failure mode: silent to the CALLER (no oracle),
                    # never silent to the operator. No address or hash is logged.
                    _log.warning("repudiation challenge capped: school_id=%s scope=%s count_24h=%s",
                                 school_id, "global" if capped_global else "per_email",
                                 global_24h if capped_global else per_email)
                    return {"request_id": request_id, "email_token": None}  # silent, same shape
                token = secrets.token_urlsafe(16)
                conn.execute(
                    "INSERT INTO repudiation_requests(id, school_id, kind, email_hash, "
                    "challenge_hash, status, created_at, expires_at) "
                    "VALUES(?,?,'email_challenge',?,?,'pending',?,?)",
                    (request_id, school_id, email_hash, _sha256(token), now,
                     now + _CHALLENGE_TTL_S))
                conn.commit()
            return {"request_id": request_id, "email_token": token}
        first, last, company = (_clean_asserted(first), _clean_asserted(last),
                                _clean_asserted(company))
        if not (first and last):
            raise GraphError("Not enough information to identify a record to remove.")
        ttl = _env_int("RM_REPUDIATE_REVIEW_TTL_DAYS", 30) * 86400
        with closing(self._conn()) as conn:
            # Anti-flood for the name path (the email path has its send caps above; this one had
            # nothing): an anonymous caller could grow the pending queue without bound, and every
            # pending row costs the coordinator a tokenizing preview. Both bounds are per-school for
            # the same reason the send cap is — one tenant must not exhaust another's DSR path.
            # A repeat of an identical pending assertion is already queued, so it collapses into the
            # existing row rather than adding one. Capped/deduped returns the SAME shape (no oracle).
            dup = conn.execute(
                "SELECT 1 FROM repudiation_requests WHERE kind='name_review' AND status='pending' "
                "AND school_id=? AND first=? AND last=? AND company=?",
                (school_id, first, last, company)).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) FROM repudiation_requests WHERE kind='name_review' "
                "AND status='pending' AND school_id=?", (school_id,)).fetchone()[0]
            capped = pending >= _env_int("RM_REPUDIATE_MAX_PENDING_REVIEWS", 200)
            if dup is not None or capped:
                if capped:
                    _log.warning("repudiation review queue full: school_id=%s pending=%s",
                                 school_id, pending)
                return {"request_id": request_id, "email_token": None}
            conn.execute(
                "INSERT INTO repudiation_requests(id, school_id, kind, first, last, company, "
                "status, created_at, expires_at) VALUES(?,?,'name_review',?,?,?,'pending',?,?)",
                (request_id, school_id, first, last, company, now, now + ttl))
            conn.commit()
        return {"request_id": request_id, "email_token": None}

    def confirm_repudiation(self, request_id: str, email: str, token: str) -> dict:
        """The email path's authorization step: re-supplying the address AND the emailed token
        proves control. Both are compared as hashes in constant time; the cleartext address was
        never at rest."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM repudiation_requests WHERE id=? AND kind='email_challenge'",
                (request_id,)).fetchone()
            if row is None or row["status"] != "pending" or row["expires_at"] < now:
                raise GraphError("That confirmation link is no longer valid.")
            ok = (hmac.compare_digest(row["email_hash"] or "", _sha256((email or "").strip().lower()))
                  and hmac.compare_digest(row["challenge_hash"] or "", _sha256(token or "")))
            if not ok:
                raise GraphError("That confirmation link is no longer valid.")
            school_id = row["school_id"]
        result = self.repudiate_execute_email(school_id, email=email)
        with closing(self._conn()) as conn:
            conn.execute(
                "UPDATE repudiation_requests SET status='confirmed', email_hash=NULL, "
                "challenge_hash=NULL, decided_at=?, purge_after=? WHERE id=?",
                (now, now + _PURGE_AFTER_S, request_id))
            conn.commit()
        return {"ok": True, "suppressed": result["suppressed"]}

    def decide_repudiation(self, school_id: int, request_id: str, coordinator_id: int,
                           approve: bool) -> dict:
        """Coordinator decision on a name_review row. School-scoped (D13): a row in another tenant
        is absent, so the route answers 404. The asserted fields are scrubbed either way — the
        decision, not the assertion, is what we keep."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM repudiation_requests WHERE id=? AND school_id=? AND kind='name_review'",
                (request_id, school_id)).fetchone()
            if row is None or row["status"] != "pending":
                raise GraphError("No such review request.")
            first, last, company = row["first"] or "", row["last"] or "", row["company"] or ""
        result = {"member_matched": False, "contacts_deleted": 0}
        if approve:
            result = self.repudiate_execute_name(school_id, first=first, last=last,
                                                 company=company)
        with closing(self._conn()) as conn:
            conn.execute(
                "UPDATE repudiation_requests SET status=?, first=NULL, last=NULL, company=NULL, "
                "decided_by=?, decided_at=?, purge_after=? WHERE id=? AND school_id=?",
                ("approved" if approve else "denied", coordinator_id, now, now + _PURGE_AFTER_S,
                 request_id, school_id))
            conn.commit()
        return {"ok": True, "status": "approved" if approve else "denied", **result}

    def list_repudiations(self, school_id: int, status: str = "pending") -> list[dict]:
        """The coordinator queue. Each row carries a NON-DESTRUCTIVE preview of what an approval
        would do, so the decision isn't blind (security L2). The preview is counts + a boolean
        only: naming the matched member to a coordinator acting on an anonymous assertion would
        itself disclose membership — the oracle the neutral 202s exist to prevent.

        The preview must agree with the executor or it is worse than no preview: an mgi-only
        member test reported `member_matched: false` for a member who never opted into discovery,
        so the coordinator approved believing no member was involved (see repudiate_execute_name).
        Both booleans are computed from the same two sources, and the index is built ONCE for the
        whole queue rather than re-scanned per row."""
        with closing(self._conn()) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, kind, first, last, company, status, created_at, expires_at "
                "FROM repudiation_requests WHERE school_id=? AND status=? AND kind='name_review' "
                "ORDER BY created_at", (school_id, status))]
            index = self._contact_index(conn, school_id) if rows else {}
            for r in rows:
                r["member_matched"], r["contact_matches"] = False, 0
                t = graph_tokens.identity_token(school_id, first=r["first"] or "",
                                                last=r["last"] or "", company=r["company"] or "")
                if t is None:
                    continue
                bucket = index.get(t[0], _EMPTY_BUCKET)
                r["member_matched"] = bool(bucket["member_uids"]) or conn.execute(
                    "SELECT 1 FROM member_graph_identity WHERE school_id=? AND identity_token=?",
                    (school_id, t[0])).fetchone() is not None
                # what an approval WOULD delete — a member match deletes nothing at all (F3)
                r["contact_matches"] = 0 if r["member_matched"] else len(bucket["contact_ids"])
        return rows


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
