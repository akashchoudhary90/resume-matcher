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

Phase-5 (docs/PHASE5.md §2.1):
  * D16 — every fold carries the SOURCE INTERACTION timestamp into `upsert_edge(seen_at=...)`.
    `last_seen_at` may only advance with the source, otherwise the A13 pre-consent guard below
    is a no-op: a rebuild (which the consent grant itself enqueues) would stamp every edge with
    `now` and float every historical interaction past the grant.
  * A13 — `promote_shareable` refuses to promote an edge whose interaction PRE-DATES the current
    `graph_discoverable` grant of either endpoint. Consent is not retroactive; a live
    relationship promotes on its next interaction.
  * A14 — only ACCEPTED interview slots fold (a declined/cancelled slot is not a relationship).
  * C3/C6 — verified check-in peer edges and attestation-pair affiliation edges. Both default to
    `pending` and traverse only under the unchanged `_SHAREABLE` predicate.
"""
from __future__ import annotations

import os
import secrets
import time
from contextlib import closing

from ..inference.redaction import redact_text
from .db import connect, migrate, platform_db_path

# Traversal-rank strengths (NOT hire probabilities). self_vouch sits at the floor so a
# self-authored vouch can't mint a top-trust edge (adversarial Sybil fix).
EDGE_STRENGTH = {
    "verified_vouch": 1.00, "interview": 0.85, "message_thread": 0.70, "mentorship": 0.75,
    "application": 0.55, "classmate": 0.50, "event_coattendance": 0.45, "org_comember": 0.45,
    "alumni_bridge": 0.40, "peer_coattendance": 0.40, "linkedin_connection": 0.30,
    "self_vouch": 0.15,
}
_VERIFIED_TIERS = ("coordinator", "alumni_verified", "employer_verified")
_MAX_EVIDENCE = 500
_AFFILIATION_TTL_S = 365 * 86400   # self-asserted data is retention-bounded, like self_upload
# C6: affiliation kind -> edge kind. A course section makes classmates; a club makes co-members.
_AFFILIATION_EDGE_KIND = {"course_section": "classmate", "club": "org_comember"}
# Erasure sentinel: postings survive an employer's erasure with created_by=0 (§6 step 26). Uid 0
# is nobody — folding an edge to it would resurrect the erased person as a graph node.
_ERASED = 0


def _peer_edge_cap() -> int:
    """D9: an unbounded fair mints O(n^2) peer edges. Read at call time so tests can move it."""
    try:
        return max(0, int(os.getenv("RM_PEER_EDGE_MAX_CHECKINS", "150")))
    except ValueError:
        return 150

# --- the set-based peer fold's SQL twin of _edge_key / upsert_edge -----------------------------
# The peer fold bypasses upsert_edge (one statement per event beats ~300k per-pair statements), so
# it has to reproduce the Python key builder EXACTLY. Both halves of the fold — the "is there work"
# probe and the upsert itself — are assembled from these fragments so they cannot drift apart, and
# tests/test_phase4_intros.py pins the key expression against _edge_key() for BOTH orderings.
# MIN/MAX (not c1/c2 positionally) is what makes the agreement with sorted() explicit rather than an
# implied consequence of the `c2.user_id > c1.user_id` enumeration predicate below: if that join
# ever loosens, an (a,b) and a (b,a) pair still collapse onto ONE edge_key instead of minting two
# edges for one relationship.
_PEER_A = "MIN(c1.user_id, c2.user_id)"
_PEER_B = "MAX(c1.user_id, c2.user_id)"
_PEER_KEY = f"{_PEER_A} || char(31) || {_PEER_B} || char(31) || 'peer_coattendance'"
_PEER_SEEN = "MAX(c1.at, c2.at)"
# Pair enumeration for ONE event (binds :event). `c2.user_id > c1.user_id` emits each unordered
# pair once and never a self-pair; the suppression filter is upsert_edge's first invariant.
_PEER_PAIRS = (
    "FROM event_checkins c1 "
    "JOIN users u1 ON u1.id=c1.user_id AND u1.role='student' "
    "JOIN event_checkins c2 ON c2.event_id=c1.event_id AND c2.user_id > c1.user_id "
    "JOIN users u2 ON u2.id=c2.user_id AND u2.role='student' "
    "WHERE c1.event_id=:event AND NOT EXISTS(SELECT 1 FROM graph_suppressions s "
    "  WHERE s.user_id=c1.user_id OR s.user_id=c2.user_id)"
)
# A pair is WORK iff the upsert would actually change a row: no edge yet, or an un-revoked edge this
# event's check-ins would advance. Mirrors the ON CONFLICT ... WHERE below term for term.
_PEER_STALE = (
    f" AND NOT EXISTS(SELECT 1 FROM graph_edges ge WHERE ge.edge_key = {_PEER_KEY} "
    f"  AND (ge.consent_state='revoked' OR ge.last_seen_at >= {_PEER_SEEN}))"
)

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
                    expires_at: float | None = None, seen_at: float | None = None) -> None:
        """Idempotent edge write on an open connection. Skips suppressed endpoints; never
        un-revokes an edge (a revoked edge stays revoked across re-runs).

        `seen_at` is the SOURCE INTERACTION time (D16) — folds pass the application/interview/
        message/check-in timestamp; interactive writers (vouch, offer-accept) pass nothing and
        get `now`. `last_seen_at` only ever moves forward, and `observation_count` only counts an
        observation that actually advanced it, so re-folding history neither inflates the count
        nor floats a stale interaction past a later consent grant (A13)."""
        if user_a == user_b:
            return
        a, b, edge_key = self._edge_key(user_a, user_b, kind)
        supp = conn.execute(
            "SELECT 1 FROM graph_suppressions WHERE user_id IN (?,?)", (a, b)).fetchone()
        if supp:
            return
        now = time.time()
        seen = seen_at if seen_at is not None else now
        w = weight if weight is not None else EDGE_STRENGTH.get(kind, 0.3)
        existing = conn.execute(
            "SELECT consent_state FROM graph_edges WHERE edge_key=?", (edge_key,)).fetchone()
        if existing is not None:
            if existing["consent_state"] == "revoked":
                return  # never resurrect a revoked edge
            conn.execute(
                "UPDATE graph_edges SET "
                "observation_count=observation_count+(CASE WHEN ?>last_seen_at THEN 1 ELSE 0 END), "
                "last_seen_at=MAX(last_seen_at,?), updated_at=?, weight=MAX(weight,?) "
                "WHERE edge_key=?", (seen, seen, now, w, edge_key))
            return
        conn.execute(
            "INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, weight, "
            "last_seen_at, provenance, provenance_ref, consent_state, owner_user_id, created_at, "
            "updated_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (secrets.token_urlsafe(10), school_id, edge_key, a, b, kind, w, seen, provenance,
             provenance_ref, consent_state, owner_user_id, now, now, expires_at))

    # ---- native folding --------------------------------------------------------------------------
    def build_native_edges(self, school_id: int) -> int:
        """Fold on-platform activity into pending native edges. Both endpoints still need
        graph_discoverable before anything is traversable; promote_shareable() flips eligible
        edges. Only interactions are used, never protected attributes (boundary #2)."""
        made = 0
        with closing(self._conn()) as conn:
            # application: applicant <-> posting.created_by (never the erasure sentinel — M5)
            for r in conn.execute(
                "SELECT a.student_id AS s, p.created_by AS e, a.created_at AS seen "
                "FROM applications a JOIN postings p ON p.id=a.posting_id "
                "WHERE p.school_id=? AND p.created_by IS NOT NULL AND p.created_by != ?",
                (school_id, _ERASED)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "application",
                                 provenance="native", seen_at=r["seen"])
                made += 1
            # interview: applicant <-> slot.proposed_by — ACCEPTED slots only (A14). A proposal the
            # student declined (or that was cancelled) is not a relationship; counting it inflated
            # pathfinder rank. Migration 004 (1b) revoked the historical bad edges.
            for r in conn.execute(
                "SELECT a.student_id AS s, i.proposed_by AS e, i.created_at AS seen "
                "FROM interview_slots i JOIN applications a ON a.id=i.application_id "
                "JOIN postings p ON p.id=a.posting_id "
                "WHERE p.school_id=? AND i.status='accepted' AND p.created_by != ?",
                (school_id, _ERASED)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "interview", provenance="native",
                                 seen_at=r["seen"])
                made += 1
            # message_thread: distinct sender pairs on the same application
            for r in conn.execute(
                "SELECT a.student_id AS s, m.sender_user_id AS e, MAX(m.sent_at) AS seen "
                "FROM messages m JOIN applications a ON a.id=m.application_id "
                "JOIN postings p ON p.id=a.posting_id "
                "WHERE p.school_id=? AND m.sender_user_id != a.student_id AND p.created_by != ? "
                "GROUP BY a.student_id, m.sender_user_id", (school_id, _ERASED)):
                self.upsert_edge(conn, school_id, r["s"], r["e"], "message_thread",
                                 provenance="native", seen_at=r["seen"])
                made += 1
            made += self._fold_event_coattendance(conn, school_id)
            made += self._fold_peer_coattendance(conn, school_id)
            made += self._fold_affiliation_edges(conn, school_id)
            conn.commit()
        return made

    def _fold_event_coattendance(self, conn, school_id: int) -> int:
        """student attendee <-> employer attendee at the same published event. C3: VERIFIED
        presence beats RSVP — an event with any check-ins folds from `event_checkins` only; the
        RSVP fold stays as the fallback for events nobody checked anyone into."""
        made = 0
        for r in conn.execute(
            "SELECT DISTINCT cs.user_id AS s, ce.user_id AS e, MAX(cs.at, ce.at) AS seen "
            "FROM event_checkins cs "
            "JOIN users us ON us.id=cs.user_id AND us.role='student' "
            "JOIN event_checkins ce ON ce.event_id=cs.event_id "
            "JOIN users ue ON ue.id=ce.user_id AND ue.role='employer' "
            "JOIN campus_events ev ON ev.id=cs.event_id WHERE ev.school_id=?", (school_id,)):
            self.upsert_edge(conn, school_id, r["s"], r["e"], "event_coattendance",
                             provenance="native", seen_at=r["seen"])
            made += 1
        for r in conn.execute(
            "SELECT DISTINCT rs.user_id AS s, re.user_id AS e, "
            "MAX(rs.created_at, re.created_at) AS seen FROM event_registrations rs "
            "JOIN event_registrations re ON re.event_id=rs.event_id "
            "JOIN campus_events ev ON ev.id=rs.event_id "
            "WHERE ev.school_id=? AND rs.role='student' AND re.role='employer' "
            "AND rs.status='registered' AND re.status='registered' "
            "AND NOT EXISTS(SELECT 1 FROM event_checkins ck WHERE ck.event_id=rs.event_id)",
            (school_id,)):
            self.upsert_edge(conn, school_id, r["s"], r["e"], "event_coattendance",
                             provenance="native", seen_at=r["seen"])
            made += 1
        return made

    def _fold_peer_coattendance(self, conn, school_id: int) -> int:
        """C3/D9: student<->student edges from VERIFIED check-ins only (an RSVP is an intention,
        not a presence). Bounded three ways:
          * only published events whose student check-in count is within RM_PEER_EDGE_MAX_CHECKINS
            (an unbounded fair would mint C(n,2) edges);
          * a watermark — an event is re-processed only when some pair at it is actually stale, so
            steady-state rebuilds touch zero events;
          * ONE set-based upsert per event instead of a per-pair Python loop (~300k statements a
            rebuild on a busy term).
        The set-based form must hold both upsert_edge invariants itself: suppressed endpoints are
        skipped, and a revoked edge is never resurrected (the ON CONFLICT ... WHERE).

        The watermark is a PER-PAIR staleness probe (_PEER_STALE), not `MAX(last_seen_at) WHERE
        provenance_ref=event`: provenance_ref records the event that FIRST minted an edge and the
        upsert deliberately never rewrites it, so a pair that also met at an earlier event leaves
        this event with no edge of its own to read a watermark from — it would COALESCE to -1 and
        re-scan the same check-ins on every single run, forever."""
        cap = _peer_edge_cap()
        now = time.time()
        events = conn.execute(
            "SELECT ev.id AS event_id FROM campus_events ev "
            "WHERE ev.school_id=? AND ev.status='published' "
            "AND (SELECT COUNT(*) FROM event_checkins ck JOIN users u ON u.id=ck.user_id "
            "     WHERE ck.event_id=ev.id AND u.role='student') BETWEEN 2 AND ?",
            (school_id, cap)).fetchall()
        made = 0
        for ev in events:
            bind = {"school": school_id, "w": EDGE_STRENGTH["peer_coattendance"],
                    "event": ev["event_id"], "now": now}
            stale = conn.execute("SELECT 1 " + _PEER_PAIRS + _PEER_STALE + " LIMIT 1",
                                 {"event": ev["event_id"]}).fetchone()
            if stale is None:
                continue                     # converged: no pair here would change
            cur = conn.execute(
                "INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, weight, "
                "observation_count, last_seen_at, provenance, provenance_ref, consent_state, "
                "created_at, updated_at) "
                f"SELECT lower(hex(randomblob(8))), :school, {_PEER_KEY}, {_PEER_A}, {_PEER_B}, "
                f"  'peer_coattendance', :w, 1, {_PEER_SEEN}, 'native', :event, 'pending', "
                "  :now, :now "
                + _PEER_PAIRS +
                " ON CONFLICT(edge_key) DO UPDATE SET "
                "  observation_count=observation_count+1, "
                "  last_seen_at=MAX(graph_edges.last_seen_at, excluded.last_seen_at), "
                "  updated_at=excluded.updated_at "
                # both invariants + D16: a no-op re-fold must not touch the row at all, else
                # observation_count inflates and `made` never reaches 0.
                "WHERE graph_edges.consent_state != 'revoked' "
                "  AND excluded.last_seen_at > graph_edges.last_seen_at", bind)
            made += cur.rowcount
        return made

    def _fold_affiliation_edges(self, conn, school_id: int) -> int:
        """C6/D15: ATTESTATION PAIRS ONLY — one edge between a confirmed claimant and the peer who
        confirmed them, and only while that confirmer's own claim on the same affiliation is itself
        confirmed. Never a clique over co-claimants: two colluders who mutually confirm gain
        exactly one edge — between themselves, which is true information (security H2)."""
        made = 0
        now = time.time()
        for r in conn.execute(
            "SELECT c.user_id AS a, c.confirmed_by AS b, c.updated_at AS seen, "
            "af.id AS aff_id, af.kind AS aff_kind FROM affiliation_claims c "
            "JOIN affiliations af ON af.id=c.affiliation_id "
            "JOIN affiliation_claims cc ON cc.affiliation_id=c.affiliation_id "
            "  AND cc.user_id=c.confirmed_by AND cc.status='confirmed' "
            "WHERE af.school_id=? AND c.status='confirmed' AND c.confirmed_by IS NOT NULL",
            (school_id,)):
            kind = _AFFILIATION_EDGE_KIND.get(r["aff_kind"])
            if kind is None:
                continue
            self.upsert_edge(conn, school_id, r["a"], r["b"], kind, provenance="affiliation",
                             provenance_ref=r["aff_id"], seen_at=r["seen"],
                             expires_at=now + _AFFILIATION_TTL_S)
            made += 1
        return made

    def promote_shareable(self, school_id: int) -> int:
        """Flip pending edges to shareable where BOTH endpoints now hold graph_discoverable and
        the edge isn't revoked/suppressed. This is how a native/self-upload edge becomes
        traversable — never at build time (adversarial 'don't default shareable').

        A13: consent is NOT retroactive. An edge whose source interaction pre-dates the CURRENT
        graph_discoverable grant of either endpoint stays pending — opting in today does not
        publish who you talked to last term. A live relationship promotes on its next interaction,
        because only a real interaction advances last_seen_at (D16)."""
        now = time.time()
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE graph_edges SET consent_state='shareable', updated_at=? "
                "WHERE school_id=? AND consent_state='pending' "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=graph_edges.user_a "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=graph_edges.user_b "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND graph_edges.last_seen_at >= (SELECT MAX(c.granted_at) FROM consents c "
                "          WHERE c.user_id=graph_edges.user_a "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND graph_edges.last_seen_at >= (SELECT MAX(c.granted_at) FROM consents c "
                "          WHERE c.user_id=graph_edges.user_b "
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
                     verify_level: str = "self", via_invite_id: str | None = None) -> dict:
        """Create a vouch and project an edge. Evidence free-text is contact-PII-redacted AT INGEST
        (before persistence). Only verified tiers project a verified_vouch edge; self → low-weight
        self_vouch.

        C7: `via_invite_id` is echoed back for the coordinator queue's display only (the
        vouch_invites row carries the link) — an invite confers NO verification tier, and neither
        does any affiliation the pair may share (D14)."""
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
        return {"vouch_id": vouch_id, "edge_kind": edge_kind, "via_invite_id": via_invite_id}

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

    # ---- B10: coordinator vouch queue ------------------------------------------------------------
    def vouches_for_coordinator(self, school_id: int, status: str = "contested") -> list[dict]:
        """The review queue. 'contested' = a subject disputed a vouch about them (the contested
        note is theirs, written to be reviewed); 'self' = active self-tier vouches a coordinator
        may choose to attest. School-scoped (D13)."""
        if status not in ("contested", "self"):
            raise RelationshipError("Unknown vouch queue.")
        where = ("v.status='contested'" if status == "contested"
                 else "v.status='active' AND v.verify_level='self'")
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT v.id, v.relationship, v.evidence_redacted, v.verify_level, v.status, "
                "v.contested_note, v.scope, v.posting_id, v.created_at, "
                "vu.email AS voucher_email, su.email AS subject_email FROM vouches v "
                "JOIN users vu ON vu.id=v.voucher_user_id "
                "JOIN users su ON su.id=v.subject_user_id "
                f"WHERE v.school_id=? AND {where} ORDER BY v.created_at DESC",
                (school_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_vouch(self, school_id: int, vouch_id: str, coordinator_id: int,
                      action: str) -> dict:
        """Resolve a queued vouch. School-scoped (D13/security C1): a vouch in another tenant is
        simply absent, so the route answers 404 rather than 403 (no cross-tenant existence oracle).

        'verify' re-activates AND upgrades the edge to the coordinator tier; 'dismiss' withdraws
        the vouch and deliberately leaves the contest-revoked edges revoked — a dismissed dispute
        does not silently republish an edge the subject objected to."""
        if action not in ("verify", "dismiss"):
            raise RelationshipError("Unknown action.")
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT id FROM vouches WHERE id=? AND school_id=?",
                               (vouch_id, school_id)).fetchone()
            if row is None:
                raise RelationshipError("No such vouch.")
        if action == "dismiss":
            with closing(self._conn()) as conn:
                conn.execute("UPDATE vouches SET status='withdrawn', updated_at=? WHERE id=? "
                             "AND school_id=?", (time.time(), vouch_id, school_id))
                conn.commit()
            return {"ok": True, "status": "withdrawn"}
        self.verify_vouch(vouch_id, coordinator_id, "coordinator")
        with closing(self._conn()) as conn:
            conn.execute("UPDATE vouches SET status='active', updated_at=? WHERE id=? "
                         "AND school_id=?", (time.time(), vouch_id, school_id))
            conn.commit()
        return {"ok": True, "status": "active", "verify_level": "coordinator"}

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
