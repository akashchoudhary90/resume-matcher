"""Relationship edges + vouches (docs/RELATIONSHIPS.md Slices AC/AF).

The ONE canonical edge table `graph_edges` is written here from consented sources:
  * native activity — applications, interviews, message threads, event co-attendance;
  * self-upload — LinkedIn matches (written by NetworkStore, promoted here);
  * vouches — a verified voucher projects a high-trust edge.

The single shared traversal predicate `_SHAREABLE` is the ONLY way any read path (pathfinder,
fairness) sees an edge: it requires the edge to be `shareable`, un-revoked, un-expired, both
endpoints to hold live `graph_discoverable` consent, and neither endpoint suppressed. No read
path may bypass it (adversarial requirement).

Revocation durability: the builder NEVER overwrites `consent_state='revoked'` and skips any
endpoint in `graph_suppressions` — a re-run can't resurrect a revoked or tombstoned edge.
"""
from __future__ import annotations

import secrets
import time
from contextlib import closing

from ..inference.redaction import redact_text
from .db import connect, migrate, platform_db_path

# Traversal-rank strengths (NOT hire probabilities). self_vouch sits at the floor so a
# self-authored vouch can't mint a top-trust edge (adversarial Sybil fix).
EDGE_STRENGTH = {
    "verified_vouch": 1.00, "interview": 0.85, "message_thread": 0.70, "application": 0.55,
    "event_coattendance": 0.45, "alumni_bridge": 0.40, "linkedin_connection": 0.30,
    "self_vouch": 0.15,
}
_VERIFIED_TIERS = ("coordinator", "alumni_verified", "employer_verified")
_MAX_EVIDENCE = 500

# The shared traversal predicate — every read path binds :now and (:uid where noted).
_SHAREABLE = (
    "ge.consent_state='shareable' AND ge.revoked_at IS NULL "
    "AND (ge.expires_at IS NULL OR ge.expires_at > :now) "
    "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=ge.user_a "
    "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
    "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=ge.user_b "
    "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
    "AND NOT EXISTS(SELECT 1 FROM graph_suppressions s "
    "          WHERE s.user_id=ge.user_a OR s.user_id=ge.user_b)"
)


class RelationshipError(Exception):
    """Client-correctable problem -> HTTP 400/409 at the route."""


class RelationshipStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    # ---- edge upsert (revocation-durable) --------------------------------------------------------
    @staticmethod
    def _edge_key(user_a: int, user_b: int, kind: str) -> tuple[int, int, str]:
        a, b = sorted((user_a, user_b))
        return a, b, f"{a}\x1f{b}\x1f{kind}"

    def upsert_edge(self, conn, school_id: int, user_a: int, user_b: int, kind: str, *,
                    provenance: str, weight: float | None = None, provenance_ref: str = "",
                    consent_state: str = "pending", owner_user_id: int | None = None,
                    expires_at: float | None = None) -> None:
        """Idempotent edge write on an open connection. Skips suppressed endpoints; never
        un-revokes an edge (a revoked edge stays revoked across re-runs)."""
        if user_a == user_b:
            return
        a, b, edge_key = self._edge_key(user_a, user_b, kind)
        supp = conn.execute(
            "SELECT 1 FROM graph_suppressions WHERE user_id IN (?,?)", (a, b)).fetchone()
        if supp:
            return
        now = time.time()
        w = weight if weight is not None else EDGE_STRENGTH.get(kind, 0.3)
        existing = conn.execute(
            "SELECT consent_state FROM graph_edges WHERE edge_key=?", (edge_key,)).fetchone()
        if existing is not None:
            if existing["consent_state"] == "revoked":
                return  # never resurrect a revoked edge
            conn.execute(
                "UPDATE graph_edges SET observation_count=observation_count+1, last_seen_at=?, "
                "updated_at=?, weight=MAX(weight,?) WHERE edge_key=?", (now, now, w, edge_key))
            return
        conn.execute(
            "INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, weight, "
            "last_seen_at, provenance, provenance_ref, consent_state, owner_user_id, created_at, "
            "updated_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (secrets.token_urlsafe(10), school_id, edge_key, a, b, kind, w, now, provenance,
             provenance_ref, consent_state, owner_user_id, now, now, expires_at))

    # ---- native folding --------------------------------------------------------------------------
    def build_native_edges(self, school_id: int) -> int:
        """Fold on-platform activity into pending native edges. Both endpoints still need
        graph_discoverable before anything is traversable; promote_shareable() flips eligible
        edges. Only interactions are used, never protected attributes (boundary #2)."""
        made = 0
        with closing(self._conn()) as conn:
            # application: applicant <-> posting.created_by
            for r in conn.execute(
                "SELECT a.student_id AS s, p.created_by AS e FROM applications a "
                "JOIN postings p ON p.id=a.posting_id WHERE p.school_id=? AND p.created_by IS NOT NULL",
                (school_id,)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "application",
                                 provenance="native")
                made += 1
            # interview: applicant <-> slot.proposed_by
            for r in conn.execute(
                "SELECT a.student_id AS s, i.proposed_by AS e FROM interview_slots i "
                "JOIN applications a ON a.id=i.application_id "
                "JOIN postings p ON p.id=a.posting_id WHERE p.school_id=?", (school_id,)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "interview", provenance="native")
                made += 1
            # message_thread: distinct sender pairs on the same application
            for r in conn.execute(
                "SELECT DISTINCT a.student_id AS s, m.sender_user_id AS e FROM messages m "
                "JOIN applications a ON a.id=m.application_id "
                "JOIN postings p ON p.id=a.posting_id "
                "WHERE p.school_id=? AND m.sender_user_id != a.student_id", (school_id,)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "message_thread",
                                 provenance="native")
                made += 1
            # event_coattendance: student attendee <-> employer attendee at the same published event
            for r in conn.execute(
                "SELECT DISTINCT rs.user_id AS s, re.user_id AS e FROM event_registrations rs "
                "JOIN event_registrations re ON re.event_id=rs.event_id "
                "JOIN campus_events ev ON ev.id=rs.event_id "
                "WHERE ev.school_id=? AND rs.role='student' AND re.role='employer' "
                "AND rs.status='registered' AND re.status='registered'", (school_id,)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "event_coattendance",
                                 provenance="native")
                made += 1
            conn.commit()
        return made

    def promote_shareable(self, school_id: int) -> int:
        """Flip pending edges to shareable where BOTH endpoints now hold graph_discoverable and
        the edge isn't revoked/suppressed. This is how a native/self-upload edge becomes
        traversable — never at build time (adversarial 'don't default shareable')."""
        now = time.time()
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE graph_edges SET consent_state='shareable', updated_at=? "
                "WHERE school_id=? AND consent_state='pending' "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=graph_edges.user_a "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=graph_edges.user_b "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND NOT EXISTS(SELECT 1 FROM graph_suppressions s "
                "          WHERE s.user_id IN (graph_edges.user_a, graph_edges.user_b))",
                (now, school_id))
            conn.commit()
            return cur.rowcount

    def revoke_edges_for(self, user_id: int) -> None:
        """Mark every edge touching a user as revoked (durable — the builder won't un-revoke)."""
        with closing(self._conn()) as conn:
            conn.execute("UPDATE graph_edges SET consent_state='revoked', revoked_at=? "
                         "WHERE user_a=? OR user_b=?", (time.time(), user_id, user_id))
            conn.commit()

    # ---- traversal (the ONE shared predicate) ----------------------------------------------------
    def neighbours(self, user_id: int, school_id: int) -> list[dict]:
        """Consented, shareable neighbours of a user. The single gate for all traversal."""
        now = time.time()
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT ge.id AS edge_id, ge.kind AS kind, ge.weight AS weight, "
                "ge.last_seen_at AS last_seen_at, "
                "CASE WHEN ge.user_a=:uid THEN ge.user_b ELSE ge.user_a END AS other "
                "FROM graph_edges ge WHERE ge.school_id=:school "
                "AND (ge.user_a=:uid OR ge.user_b=:uid) AND " + _SHAREABLE,
                {"uid": user_id, "school": school_id, "now": now}).fetchall()
        return [dict(r) for r in rows]

    # ---- vouches (Slice AF) ----------------------------------------------------------------------
    def create_vouch(self, *, school_id: int, voucher_user_id: int, subject_user_id: int,
                     relationship: str | None, evidence: str = "", scope: str = "general",
                     posting_id: str | None = None, org_id: int | None = None,
                     verify_level: str = "self") -> dict:
        """Create a vouch and project an edge. Evidence free-text is contact-PII-redacted AT INGEST
        (before persistence). Only verified tiers project a verified_vouch edge; self → low-weight
        self_vouch."""
        if voucher_user_id == subject_user_id:
            raise RelationshipError("You can't vouch for yourself.")
        rel_ok = relationship in ("worked_together", "managed_them", "ta_instructor", "classmate",
                                  "mentored_them", "other", None)
        if not rel_ok:
            raise RelationshipError("Unknown relationship.")
        evidence_redacted = redact_text((evidence or "")[:_MAX_EVIDENCE]) if evidence else None
        vouch_id = secrets.token_urlsafe(10)
        now = time.time()
        edge_kind = "verified_vouch" if verify_level in _VERIFIED_TIERS else "self_vouch"
        with closing(self._conn()) as conn:
            # anti-Sybil: cap active vouches authored per voucher per rolling day
            recent = conn.execute(
                "SELECT COUNT(*) FROM vouches WHERE voucher_user_id=? AND created_at > ?",
                (voucher_user_id, now - 86400)).fetchone()[0]
            if recent >= 20:
                raise RelationshipError("Too many vouches today — try again tomorrow.")
            conn.execute(
                "INSERT INTO vouches(id, school_id, voucher_user_id, subject_user_id, scope, "
                "posting_id, org_id, relationship, evidence_redacted, verify_level, created_at, "
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (vouch_id, school_id, voucher_user_id, subject_user_id, scope, posting_id, org_id,
                 relationship, evidence_redacted, verify_level, now, now))
            self.upsert_edge(conn, school_id, voucher_user_id, subject_user_id, edge_kind,
                             provenance="vouch", provenance_ref=vouch_id, consent_state="pending")
            conn.commit()
        return {"vouch_id": vouch_id, "edge_kind": edge_kind}

    def verify_vouch(self, vouch_id: str, verifier_user_id: int, verify_level: str) -> dict:
        if verify_level not in _VERIFIED_TIERS:
            raise RelationshipError("Unknown verification level.")
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT school_id, voucher_user_id, subject_user_id FROM vouches "
                               "WHERE id=?", (vouch_id,)).fetchone()
            if row is None:
                raise RelationshipError("No such vouch.")
            conn.execute("UPDATE vouches SET verify_level=?, verified_by_user_id=?, verified_at=?, "
                         "updated_at=? WHERE id=?",
                         (verify_level, verifier_user_id, now, now, vouch_id))
            # upgrade the projected edge to verified_vouch (drop the self_vouch)
            _, _, self_key = self._edge_key(row["voucher_user_id"], row["subject_user_id"],
                                            "self_vouch")
            conn.execute("UPDATE graph_edges SET consent_state='revoked', revoked_at=? "
                         "WHERE edge_key=?", (now, self_key))
            self.upsert_edge(conn, row["school_id"], row["voucher_user_id"], row["subject_user_id"],
                             "verified_vouch", provenance="vouch", provenance_ref=vouch_id,
                             consent_state="pending")
            conn.commit()
        return {"ok": True, "verify_level": verify_level}

    def contest_vouch(self, vouch_id: str, subject_user_id: int, note: str = "") -> dict:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT subject_user_id FROM vouches WHERE id=?",
                               (vouch_id,)).fetchone()
            if row is None or row["subject_user_id"] != subject_user_id:
                raise RelationshipError("No such vouch.")
            conn.execute("UPDATE vouches SET status='contested', contested_note=?, updated_at=? "
                         "WHERE id=?", (redact_text((note or "")[:_MAX_EVIDENCE]), time.time(),
                                        vouch_id))
            # contested vouches are excluded from traversal until resolved
            conn.execute("UPDATE graph_edges SET consent_state='revoked', revoked_at=? "
                         "WHERE provenance_ref=?", (time.time(), vouch_id))
            conn.commit()
        return {"ok": True, "status": "contested"}

    def vouches_about(self, subject_user_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT v.id, v.relationship, v.evidence_redacted, v.verify_level, v.status, "
                "v.scope, v.posting_id, v.created_at, u.email AS voucher_email, u.role AS voucher_role "
                "FROM vouches v JOIN users u ON u.id=v.voucher_user_id "
                "WHERE v.subject_user_id=? ORDER BY v.created_at DESC", (subject_user_id,)).fetchall()
        return [dict(r) for r in rows]

    def vouches_for_subject_on_posting(self, subject_user_id: int,
                                       posting_id: str) -> list[dict]:
        """Active, non-contested vouches shown on the employer evidence card."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT v.relationship, v.evidence_redacted, v.verify_level, v.created_at, "
                "u.role AS voucher_role FROM vouches v JOIN users u ON u.id=v.voucher_user_id "
                "WHERE v.subject_user_id=? AND v.status='active' "
                "AND (v.scope='general' OR v.posting_id=?) ORDER BY v.created_at DESC",
                (subject_user_id, posting_id)).fetchall()
        return [dict(r) for r in rows]
