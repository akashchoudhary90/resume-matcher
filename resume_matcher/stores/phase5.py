"""Phase-5 stores: mentorship, affiliations, vouch invites, employer contacts + ERM
(docs/PHASE5.md §2.13).

Design stances, each load-bearing against a named adversarial finding:
  * **Mentorship is double-opt-in on the mentor side (C4/D1).** The `mentor_profiles` ROW is the
    opt-in record — deleting it is the revoke, so no new consent purpose was added. Matching
    additionally requires live `warm_intro` + `graph_discoverable`. An offer only becomes an edge
    when the MENTOR accepts, and the edge is still `pending` (both endpoints must be discoverable
    before it traverses). A decline is silent to the student AND invisible to coordinators (D8):
    `mentorship_stats` is the only coordinator telemetry and it is MIN_CELL'd aggregates, plus a
    pair cooldown so a decline can't be probed by re-offering (privacy F9).
  * **Affiliations are self-asserted and confirmed BY LINK, never by search (C6/D15).** There is
    no member-search endpoint anywhere in Phase 5. A claimant shares their OWN claim's confirm URL
    out-of-band; `claimants()` is gated on holding a CONFIRMED claim and masks emails outside an
    attestation pair, because an any-status viewer gate over a 300-person course would be a mass
    email-enumeration oracle (privacy F1). `claim_role` is DISPLAY-ONLY (D14): nothing here or
    anywhere branches on it — a self-asserted "instructor" must not manufacture authority.
  * **Vouch invites are capability links (C7/D10).** Tokens are sha256 at rest (feasibility L2)
    and a cross-school voucher is REJECTED rather than remapped (security M1).
  * **Employer contacts are business contacts (C5, PIPEDA exemption).** Free text is capped +
    `redact_text()`'d + escaped + CSV-neutralized AT WRITE, so no email/phone can survive into a
    field the erasure/repudiation machinery cannot reach (privacy F5); `contact_user_id` must be
    an own-org member (security M4).

Every coordinator-reachable read/mutation takes `school_id` and enforces `AND school_id=?` (D13) —
cross-tenant ids look absent so routes answer 404, never 403.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import time
from contextlib import closing

from ..inference.redaction import redact_text
from .db import connect, migrate, platform_db_path
from .relationships import RelationshipStore

# Local, deliberately NOT imported from audit_store/data_planes: the two planes stay unlinked
# (boundary #2) and this module must never pull the audit plane into the platform plane.
MIN_CELL = 5
_OFFER_TTL_S = 30 * 86400
_INVITE_TTL_S = 30 * 86400
_MAX_OPEN_INVITES = 10
_MAX_TOPICS = 200
_MAX_LABEL = 120
_MAX_CONTACT_FIELD = 120
_RELATIONSHIP_HINTS = ("worked_together", "managed_them", "ta_instructor", "classmate",
                       "mentored_them", "other")
_CLAIM_ROLES = ("member", "ta", "instructor", "exec")
_AFFILIATION_KINDS = ("course_section", "club")
# A leading one of these makes Excel/Sheets treat a cell as a formula; contacts land in coordinator
# CSV exports, so they are neutralized at write, not at render.
_CSV_TRIGGER = ("=", "+", "-", "@", "\t", "\r")


class Phase5Error(Exception):
    """Client-correctable problem -> HTTP 400/404/409 at the route."""


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _safe_text(value: str, limit: int) -> str:
    """The write-side chokepoint for third-party free text (privacy F5): cap -> redact contact PII
    -> HTML-escape -> CSV-neutralize. Order matters: redaction runs on the raw text (escaping first
    would hide an address from the redactor behind entities)."""
    out = redact_text((value or "").strip()[:limit])
    out = html.escape(out)
    return ("'" + out) if out[:1] in _CSV_TRIGGER else out


def _mask_email(email: str) -> str:
    """'alex@yorku.ca' -> 'a***@yorku.ca'. Enough to recognize someone you already know, not
    enough to harvest (privacy F1)."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def _cell(n: int) -> int | None:
    """MIN_CELL suppression on an aggregate egress: a count below the floor is not reported."""
    return n if n >= MIN_CELL else None


def _norm_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (label or "").lower())


def _hash_token(token: str) -> str:
    """Invite links are capabilities: only the digest is ever at rest (D10/feasibility L2)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class _Store:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)


def _enqueue_build_edges(school_id: int) -> None:
    """Best-effort: a fold that doesn't run yet is a delay, never a correctness bug (the next
    rebuild picks it up). Imported locally to keep the worker graph out of store import time."""
    try:
        from ..workers.runner import JobStore
        JobStore().enqueue("build_edges", {"school_id": school_id},
                           dedupe_key=f"build_edges:{school_id}")
    except Exception:  # pragma: no cover - queueing is advisory
        pass


# ---- C4: mentorship --------------------------------------------------------------------------------
class MentorStore(_Store):
    def upsert_profile(self, user_id: int, school_id: int, *, program: str, topics: str,
                       capacity: int, active: bool) -> dict:
        """The row IS the opt-in record (D1). program/topics are structural matching keys, so they
        go through redact_text() at ingest like every other free-text field."""
        cap = max(1, min(int(capacity or 1), 20))
        now = time.time()
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO mentor_profiles(user_id, school_id, program, topics, capacity, "
                "active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET program=excluded.program, "
                "topics=excluded.topics, capacity=excluded.capacity, active=excluded.active, "
                "updated_at=excluded.updated_at",
                (user_id, school_id, redact_text((program or "").strip()[:_MAX_TOPICS]),
                 redact_text((topics or "").strip()[:_MAX_TOPICS]), cap, 1 if active else 0,
                 now, now))
            conn.commit()
        return self.get_profile(user_id) or {}

    def delete_profile(self, user_id: int) -> bool:
        """The opt-out (D1): deleting the row is the revoke — no tombstone, no soft flag."""
        with closing(self._conn()) as conn:
            cur = conn.execute("DELETE FROM mentor_profiles WHERE user_id=?", (user_id,))
            conn.commit()
            return bool(cur.rowcount)

    def get_profile(self, user_id: int) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM mentor_profiles WHERE user_id=?",
                               (user_id,)).fetchone()
        return dict(row) if row else None

    def eligible_mentors(self, school_id: int) -> list[dict]:
        """A mentor must clear every gate at once: an active profile (the opt-in), LIVE warm_intro
        AND graph_discoverable consent, standing to mentor (a VERIFIED alum — self-claimed is not
        enough — or staff/employer), and remaining capacity."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT u.id AS user_id, u.email, u.alumni_status, mp.program, mp.topics, "
                "mp.capacity, (mp.capacity - (SELECT COUNT(*) FROM mentorship_offers mo "
                "  WHERE mo.mentor_user_id=u.id AND mo.status='accepted')) AS capacity_left "
                "FROM mentor_profiles mp JOIN users u ON u.id=mp.user_id "
                "WHERE mp.school_id=? AND u.school_id=? AND mp.active=1 "
                "AND (u.alumni_status='verified' OR u.role IN ('employer','coordinator')) "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=u.id "
                "          AND c.purpose='warm_intro' AND c.revoked_at IS NULL) "
                "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=u.id "
                "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
                "AND (mp.capacity - (SELECT COUNT(*) FROM mentorship_offers mo "
                "  WHERE mo.mentor_user_id=u.id AND mo.status='accepted')) > 0 "
                "ORDER BY capacity_left DESC, u.id", (school_id, school_id)).fetchall()
        return [dict(r) for r in rows]

    def create_offer(self, *, school_id: int, student_user_id: int, mentor_user_id: int,
                     origin: str, rationale: str) -> dict:
        """D13/security C1: BOTH users must belong to school_id — a coordinator cannot offer across
        tenants (the store, not the route, is the invariant).

        The cooldown and the open-offer refusal share ONE message on purpose (D8): if "already
        offered" and "recently declined" were distinguishable, re-offering would become a decline
        oracle and the mentor's silent decline would leak."""
        if origin not in ("matcher", "coordinator"):
            raise Phase5Error("Unknown offer origin.")
        if student_user_id == mentor_user_id:
            raise Phase5Error("An intro through this connection isn't available.")
        now = time.time()
        cooldown = _env_int("RM_MENTOR_OFFER_COOLDOWN_DAYS", 90) * 86400
        offer_id = secrets.token_urlsafe(10)
        with closing(self._conn()) as conn:
            known = conn.execute(
                "SELECT COUNT(*) FROM users WHERE id IN (?,?) AND school_id=?",
                (student_user_id, mentor_user_id, school_id)).fetchone()[0]
            if known != 2:
                raise Phase5Error("No such user.")
            recent = conn.execute(
                "SELECT 1 FROM mentorship_offers WHERE student_user_id=? AND mentor_user_id=? "
                "AND (status='offered' OR created_at > ?)",
                (student_user_id, mentor_user_id, now - cooldown)).fetchone()
            if recent:
                raise Phase5Error("A mentorship offer for this pair isn't available right now.")
            conn.execute(
                "INSERT INTO mentorship_offers(id, school_id, student_user_id, mentor_user_id, "
                "origin, rationale, status, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?,'offered',?,?)",
                (offer_id, school_id, student_user_id, mentor_user_id, origin,
                 redact_text((rationale or "").strip()[:_MAX_TOPICS]), now, now + _OFFER_TTL_S))
            conn.commit()
        return {"offer_id": offer_id, "status": "offered"}

    def offers_for_mentor(self, mentor_user_id: int) -> list[dict]:
        """The mentor's inbox — the student's identity is revealed HERE and only here, mirroring
        the broker-inbox reveal discipline (the offer is the ask; the mentor must know who)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT mo.id, mo.rationale, mo.created_at, mo.expires_at, u.email AS student_email, "
                "sp.program FROM mentorship_offers mo JOIN users u ON u.id=mo.student_user_id "
                "LEFT JOIN student_profiles sp ON sp.user_id=mo.student_user_id "
                "WHERE mo.mentor_user_id=? AND mo.status='offered' ORDER BY mo.created_at",
                (mentor_user_id,)).fetchall()
        return [dict(r) for r in rows]

    def respond_offer(self, offer_id: str, mentor_user_id: int, accept: bool) -> dict:
        """Mentor-only (the identity check mirrors the intro broker's). Accept mints a PENDING
        mentorship edge — acceptance is consent to be connected, not consent to be discoverable;
        promote_shareable still needs both endpoints' graph_discoverable."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM mentorship_offers WHERE id=?", (offer_id,)).fetchone()
            if row is None or row["mentor_user_id"] != mentor_user_id:
                raise Phase5Error("No such offer.")
            if row["status"] != "offered":
                raise Phase5Error(f"That offer is already {row['status']}.")
            conn.execute("UPDATE mentorship_offers SET status=?, responded_at=? WHERE id=?",
                         ("accepted" if accept else "declined", now, offer_id))
            conn.commit()
            school_id, student = row["school_id"], row["student_user_id"]
        if not accept:
            return {"status": "declined"}   # D8: no notification to anyone, no coordinator surface
        rel = RelationshipStore(self.path)
        with closing(self._conn()) as conn:
            rel.upsert_edge(conn, school_id, student, mentor_user_id, "mentorship",
                            provenance="alumni", provenance_ref=offer_id, consent_state="pending")
            conn.commit()
        rel.promote_shareable(school_id)
        return {"status": "accepted", "student_user_id": student, "school_id": school_id}

    def mentorship_stats(self, school_id: int) -> dict:
        """privacy F9/D8: the ONLY coordinator-visible mentorship telemetry. Aggregates only, each
        suppressed below MIN_CELL — with per-offer status a coordinator could diff the offer they
        made against the accepted count and infer the mentor's silent decline."""
        with closing(self._conn()) as conn:
            offers = conn.execute("SELECT COUNT(*) FROM mentorship_offers WHERE school_id=?",
                                  (school_id,)).fetchone()[0]
            accepted = conn.execute(
                "SELECT COUNT(*) FROM mentorship_offers WHERE school_id=? AND status='accepted'",
                (school_id,)).fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM mentor_profiles WHERE school_id=? AND active=1",
                (school_id,)).fetchone()[0]
        return {"offers_made": _cell(offers), "accepted": _cell(accepted),
                "active_mentors": _cell(active), "min_cell": MIN_CELL}

    def sweep_expired(self) -> int:
        now = time.time()
        with closing(self._conn()) as conn:
            cur = conn.execute("UPDATE mentorship_offers SET status='expired', responded_at=? "
                               "WHERE status='offered' AND expires_at < ?", (now, now))
            conn.commit()
            return cur.rowcount


# ---- C6: affiliations ------------------------------------------------------------------------------
class AffiliationStore(_Store):
    def claim(self, *, user_id: int, school_id: int, kind: str, label: str, term: str = "",
              claim_role: str = "member") -> dict:
        """Self-disclosure: the claimant says which section/club they were in. Confers nothing on
        its own — an unconfirmed claim has zero read visibility and mints zero edges."""
        if kind not in _AFFILIATION_KINDS:
            raise Phase5Error("Unknown affiliation kind.")
        if claim_role not in _CLAIM_ROLES:
            raise Phase5Error("Unknown claim role.")
        norm = _norm_label(label)
        if not norm:
            raise Phase5Error("That label isn't usable.")
        term_norm = _norm_label(term)
        label_norm = f"{norm}:{term_norm}" if term_norm else norm
        now = time.time()
        with closing(self._conn()) as conn:
            held = conn.execute(
                "SELECT COUNT(*) FROM affiliation_claims WHERE user_id=? AND status != 'removed'",
                (user_id,)).fetchone()[0]
            if held >= _env_int("RM_AFFILIATION_MAX_CLAIMS", 30):
                raise Phase5Error("You have too many class/club claims.")
            row = conn.execute(
                "SELECT id FROM affiliations WHERE school_id=? AND kind=? AND label_norm=?",
                (school_id, kind, label_norm)).fetchone()
            if row is None:
                aff_id = secrets.token_urlsafe(10)
                conn.execute(
                    "INSERT INTO affiliations(id, school_id, kind, label_norm, label_display, "
                    "term, created_at) VALUES(?,?,?,?,?,?,?)",
                    (aff_id, school_id, kind, label_norm, _safe_text(label, _MAX_LABEL),
                     _safe_text(term, 40), now))
            else:
                aff_id = row["id"]
            existing = conn.execute(
                "SELECT id FROM affiliation_claims WHERE affiliation_id=? AND user_id=?",
                (aff_id, user_id)).fetchone()
            if existing is not None:
                raise Phase5Error("You already claimed that one.")
            claim_id = secrets.token_urlsafe(16)   # the confirm-link capability (D15)
            conn.execute(
                "INSERT INTO affiliation_claims(id, affiliation_id, user_id, claim_role, status, "
                "created_at, updated_at) VALUES(?,?,?,?,'unconfirmed',?,?)",
                (claim_id, aff_id, user_id, claim_role, now, now))
            conn.commit()
        return {"claim_id": claim_id, "affiliation_id": aff_id, "status": "unconfirmed",
                "confirm_url": f"/student#affil-confirm={claim_id}"}

    def mine(self, user_id: int) -> list[dict]:
        """Own claims + each claim's confirm_url. The claimant shares THEIR OWN link out-of-band
        with someone who can attest them (D15) — the platform never lists strangers to solicit."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT c.id AS claim_id, c.status, c.claim_role, c.confirmed_by, c.created_at, "
                "af.id AS affiliation_id, af.kind, af.label_display, af.term "
                "FROM affiliation_claims c JOIN affiliations af ON af.id=c.affiliation_id "
                "WHERE c.user_id=? AND c.status != 'removed' ORDER BY c.created_at DESC",
                (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["confirm_url"] = f"/student#affil-confirm={d['claim_id']}"
            d["confirmed_by"] = bool(d["confirmed_by"])   # a boolean: never name the attester here
            out.append(d)
        return out

    def claimants(self, affiliation_id: str, viewer_user_id: int) -> list[dict]:
        """privacy F1 (hard requirement): a CONFIRMED claim is the price of admission, and even
        then only CONFIRMED co-claimants are listed, with masked emails. An unconfirmed claim
        grants ZERO visibility — otherwise anyone could type 'CSC369' and harvest 300 addresses.
        `email_masked` carries the FULL address only inside an attestation pair (the two already
        know each other by construction)."""
        with closing(self._conn()) as conn:
            viewer = conn.execute(
                "SELECT id FROM affiliation_claims WHERE affiliation_id=? AND user_id=? "
                "AND status='confirmed'", (affiliation_id, viewer_user_id)).fetchone()
            if viewer is None:
                raise Phase5Error("No such affiliation.")   # 404: absent, not forbidden
            rows = conn.execute(
                "SELECT c.id AS claim_id, c.user_id, c.claim_role, c.status, c.confirmed_by, "
                "u.email FROM affiliation_claims c JOIN users u ON u.id=c.user_id "
                "WHERE c.affiliation_id=? AND c.status='confirmed' AND c.user_id != ? "
                "ORDER BY c.created_at", (affiliation_id, viewer_user_id)).fetchall()
            viewer_claim = conn.execute(
                "SELECT confirmed_by FROM affiliation_claims WHERE id=?",
                (viewer["id"],)).fetchone()
        attested_by_viewer = {r["user_id"] for r in rows if r["confirmed_by"] == viewer_user_id}
        confirmed_viewer = viewer_claim["confirmed_by"] if viewer_claim else None
        out = []
        for r in rows:
            pair = r["user_id"] in attested_by_viewer or r["user_id"] == confirmed_viewer
            out.append({"claim_id": r["claim_id"], "claim_role": r["claim_role"],
                        "status": r["status"],
                        "email_masked": r["email"] if pair else _mask_email(r["email"])})
        return out

    def confirm(self, claim_id: str, confirmer_user_id: int) -> dict:
        """Reached via confirm-link only (claim_id IS the capability). Two shapes:
          * the confirmer's own claim is already confirmed -> the target flips immediately;
          * BOTH unconfirmed (bootstrap) -> the first direction only records `confirmed_by`; when
            the reciprocal confirm lands, both flip together. So a single account can never
            confirm anyone: it takes a mutual pair, and under D15 that pair gains exactly ONE edge
            — between themselves, which is true (security H2).
        The daily cap bounds how fast one account can manufacture attestations."""
        now = time.time()
        with closing(self._conn()) as conn:
            target = conn.execute(
                "SELECT c.*, af.school_id AS school_id FROM affiliation_claims c "
                "JOIN affiliations af ON af.id=c.affiliation_id WHERE c.id=?",
                (claim_id,)).fetchone()
            if target is None or target["status"] == "removed":
                raise Phase5Error("No such claim.")
            if target["user_id"] == confirmer_user_id:
                raise Phase5Error("You can't confirm your own claim.")
            mine_row = conn.execute(
                "SELECT * FROM affiliation_claims WHERE affiliation_id=? AND user_id=? "
                "AND status != 'removed'",
                (target["affiliation_id"], confirmer_user_id)).fetchone()
            if mine_row is None:
                # you can only attest a section/club you claim yourself
                raise Phase5Error("No such claim.")
            done_today = conn.execute(
                "SELECT COUNT(*) FROM affiliation_claims WHERE confirmed_by=? AND updated_at > ?",
                (confirmer_user_id, now - 86400)).fetchone()[0]
            if done_today >= _env_int("RM_AFFILIATION_MAX_CONFIRMS_PER_DAY", 20):
                raise Phase5Error("Too many confirmations today — try again tomorrow.")
            if target["status"] == "confirmed" and target["confirmed_by"] is not None:
                return {"status": "confirmed"}
            flipped = False
            if mine_row["status"] == "confirmed":
                conn.execute("UPDATE affiliation_claims SET status='confirmed', confirmed_by=?, "
                             "updated_at=? WHERE id=?", (confirmer_user_id, now, claim_id))
                flipped = True
            elif mine_row["confirmed_by"] == target["user_id"]:
                # the reciprocal half of a bootstrap: both sides have now attested each other
                conn.execute("UPDATE affiliation_claims SET status='confirmed', confirmed_by=?, "
                             "updated_at=? WHERE id=?", (confirmer_user_id, now, claim_id))
                conn.execute("UPDATE affiliation_claims SET status='confirmed', updated_at=? "
                             "WHERE id=?", (now, mine_row["id"]))
                flipped = True
            else:
                conn.execute("UPDATE affiliation_claims SET confirmed_by=?, updated_at=? "
                             "WHERE id=?", (confirmer_user_id, now, claim_id))
            conn.commit()
            school_id, status = target["school_id"], ("confirmed" if flipped else "unconfirmed")
        if flipped:
            _enqueue_build_edges(school_id)
        return {"status": status}

    def remove_claim(self, claim_id: str, user_id: int) -> bool:
        """Own claim only. The derived edges are HARD-deleted, not revoked (feasibility L3): a
        revoke is permanent by design, and someone who mis-typed a section then genuinely re-claims
        and re-confirms should get their edge back."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT affiliation_id FROM affiliation_claims WHERE id=? AND user_id=?",
                (claim_id, user_id)).fetchone()
            if row is None:
                return False
            conn.execute(
                "DELETE FROM graph_edges WHERE kind IN ('classmate','org_comember') "
                "AND provenance_ref=? AND (user_a=? OR user_b=?)",
                (row["affiliation_id"], user_id, user_id))
            # Peers I attested keep their own confirmed claims: withdrawing my claim must not be a
            # way to strip someone else's standing. The fold's own guard (a confirmer's claim must
            # still be confirmed) is what stops my deleted claim from re-minting their edge.
            conn.execute("DELETE FROM affiliation_claims WHERE id=? AND user_id=?",
                         (claim_id, user_id))
            conn.commit()
        return True


# ---- C7: vouch invites -----------------------------------------------------------------------------
class VouchInviteStore(_Store):
    def create(self, *, subject_user_id: int, school_id: int,
               relationship_hint: str | None) -> dict:
        """The subject asks to be vouched-about; the ask IS the consent. The cleartext token is
        returned ONCE — only its sha256 persists, so a DB read can't mint vouches (feasibility L2)."""
        if relationship_hint is not None and relationship_hint not in _RELATIONSHIP_HINTS:
            raise Phase5Error("Unknown relationship.")
        now = time.time()
        with closing(self._conn()) as conn:
            open_now = conn.execute(
                "SELECT COUNT(*) FROM vouch_invites WHERE subject_user_id=? AND status='open' "
                "AND expires_at > ?", (subject_user_id, now)).fetchone()[0]
            if open_now >= _MAX_OPEN_INVITES:
                raise Phase5Error("You have too many open invite links.")
            token = secrets.token_urlsafe(16)
            expires_at = now + _INVITE_TTL_S
            conn.execute(
                "INSERT INTO vouch_invites(token_hash, school_id, subject_user_id, "
                "relationship_hint, status, created_at, expires_at) VALUES(?,?,?,?,'open',?,?)",
                (_hash_token(token), school_id, subject_user_id, relationship_hint, now,
                 expires_at))
            conn.commit()
        return {"invite_token": token, "expires_at": expires_at,
                "invite_url": f"/student#vouch-invite={token}"}

    def get_open(self, token: str, school_id: int) -> dict | None:
        """Hash lookup, school-scoped: an invite from another tenant is simply absent (security
        M1) — the route 404s rather than revealing that the link is real but foreign."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT vi.*, u.email AS subject_email FROM vouch_invites vi "
                "JOIN users u ON u.id=vi.subject_user_id "
                "WHERE vi.token_hash=? AND vi.school_id=? AND vi.status='open' "
                "AND vi.expires_at > ?", (_hash_token(token), school_id, time.time())).fetchone()
        if row is None:
            return None
        d = dict(row)
        d.pop("token_hash", None)   # never hand the at-rest handle back out
        return d

    def consume(self, token: str, voucher_user_id: int, voucher_school_id: int,
                vouch_id: str) -> None:
        """security M1 — decided: REJECT a cross-school voucher, never remap. The vouch and any
        derived edge always live in the INVITE's school; silently re-homing the voucher would put
        an edge in a graph they are not a member of."""
        now = time.time()
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM vouch_invites WHERE token_hash=? AND status='open' "
                "AND expires_at > ?", (_hash_token(token), now)).fetchone()
            if row is None or row["school_id"] != voucher_school_id:
                raise Phase5Error("That invite link is no longer valid.")
            if row["subject_user_id"] == voucher_user_id:
                raise Phase5Error("That invite link is no longer valid.")
            conn.execute("UPDATE vouch_invites SET status='used', used_by=?, used_at=?, vouch_id=? "
                         "WHERE token_hash=?",
                         (voucher_user_id, now, vouch_id, row["token_hash"]))
            conn.commit()

    def open_for_subject(self, subject_user_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT relationship_hint, created_at, expires_at FROM vouch_invites "
                "WHERE subject_user_id=? AND status='open' AND expires_at > ? "
                "ORDER BY created_at DESC", (subject_user_id, time.time())).fetchall()
        return [dict(r) for r in rows]

    def revoke(self, token_or_id: str, subject_user_id: int) -> bool:
        """The subject withdraws the ask. Accepts the cleartext token (what the subject holds)."""
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE vouch_invites SET status='revoked' WHERE token_hash IN (?,?) "
                "AND subject_user_id=? AND status='open'",
                (_hash_token(token_or_id), token_or_id, subject_user_id))
            conn.commit()
            return bool(cur.rowcount)

    def sweep_expired(self) -> int:
        with closing(self._conn()) as conn:
            cur = conn.execute("UPDATE vouch_invites SET status='expired' WHERE status='open' "
                               "AND expires_at < ?", (time.time(),))
            conn.commit()
            return cur.rowcount


# ---- C5: employer contacts + coordinator ERM -------------------------------------------------------
class ContactStore(_Store):
    def add_contact(self, *, org_id: int, school_id: int, added_by: int, display_label: str,
                    role_title: str, contact_user_id: int | None) -> dict:
        """PIPEDA business-contact exemption: an employer naming its OWN hiring manager. Free-text
        contacts (contact_user_id IS NULL) may hold business role/title text ONLY — _safe_text
        strips contact PII at write so nothing lands here that erasure/repudiation can't reach
        (privacy F5). security M4: a supplied contact_user_id must be an own-org member."""
        label = _safe_text(display_label, _MAX_CONTACT_FIELD)
        if not label:
            raise Phase5Error("A contact needs a label.")
        if contact_user_id is not None:
            with closing(self._conn()) as conn:
                ok = conn.execute("SELECT 1 FROM users WHERE id=? AND org_id=? AND school_id=?",
                                  (contact_user_id, org_id, school_id)).fetchone()
            if ok is None:
                raise Phase5Error("That contact isn't a member of your organization.")
        contact_id = secrets.token_urlsafe(10)
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO employer_contacts(id, school_id, org_id, display_label, role_title, "
                "contact_user_id, added_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (contact_id, school_id, org_id, label,
                 _safe_text(role_title, _MAX_CONTACT_FIELD), contact_user_id, added_by,
                 time.time()))
            conn.commit()
        return {"contact_id": contact_id, "display_label": label}

    def list_contacts(self, org_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT id AS contact_id, display_label, role_title, contact_user_id, created_at "
                "FROM employer_contacts WHERE org_id=? ORDER BY created_at", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_contact(self, contact_id: str, org_id: int) -> bool:
        """THE C5 deletion path: the row AND every posting that pointed at it. Without the cascade
        a deleted contact would keep steering the pathfinder through posting_contacts."""
        with closing(self._conn()) as conn:
            owned = conn.execute("SELECT 1 FROM employer_contacts WHERE id=? AND org_id=?",
                                 (contact_id, org_id)).fetchone()
            if owned is None:
                return False
            conn.execute("DELETE FROM posting_contacts WHERE employer_contact_id=?", (contact_id,))
            conn.execute("DELETE FROM employer_contacts WHERE id=? AND org_id=?",
                         (contact_id, org_id))
            conn.commit()
        return True

    def set_posting_contact(self, *, posting_id: str, school_id: int, added_by: int,
                            contact_user_id: int | None = None,
                            employer_contact_id: str | None = None,
                            relation: str = "hiring_manager") -> dict:
        """One target per posting: this REPLACES any existing row. The pathfinder aims at whoever
        is named here, so the own-org membership check (security M4) is what stops a posting from
        pointing the graph at an arbitrary user."""
        if relation not in ("hiring_manager", "recruiter", "referrer", "team_member"):
            raise Phase5Error("Unknown relation.")
        if (contact_user_id is None) == (employer_contact_id is None):
            raise Phase5Error("Name exactly one contact.")
        with closing(self._conn()) as conn:
            posting = conn.execute("SELECT org_id FROM postings WHERE id=? AND school_id=?",
                                   (posting_id, school_id)).fetchone()
            if posting is None:
                raise Phase5Error("No such posting.")
            if contact_user_id is not None:
                ok = conn.execute("SELECT 1 FROM users WHERE id=? AND org_id=? AND school_id=?",
                                  (contact_user_id, posting["org_id"], school_id)).fetchone()
                if ok is None:
                    raise Phase5Error("That contact isn't a member of your organization.")
            else:
                ok = conn.execute("SELECT 1 FROM employer_contacts WHERE id=? AND org_id=?",
                                  (employer_contact_id, posting["org_id"])).fetchone()
                if ok is None:
                    raise Phase5Error("No such contact.")
            row_id = secrets.token_urlsafe(10)
            conn.execute("DELETE FROM posting_contacts WHERE posting_id=?", (posting_id,))
            conn.execute(
                "INSERT INTO posting_contacts(id, school_id, posting_id, contact_user_id, "
                "employer_contact_id, relation, added_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (row_id, school_id, posting_id, contact_user_id, employer_contact_id, relation,
                 added_by, time.time()))
            conn.commit()
        return {"posting_contact_id": row_id, "relation": relation}

    def clear_posting_contact(self, posting_id: str) -> bool:
        with closing(self._conn()) as conn:
            cur = conn.execute("DELETE FROM posting_contacts WHERE posting_id=?", (posting_id,))
            conn.commit()
            return bool(cur.rowcount)

    def contacts_for_user(self, user_id: int) -> int:
        """Erasure hook (§6 step 20): a member's contact rows die with the person, along with the
        posting_contacts rows referencing them."""
        with closing(self._conn()) as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM employer_contacts WHERE contact_user_id=?", (user_id,))]
            for cid in ids:
                conn.execute("DELETE FROM posting_contacts WHERE employer_contact_id=?", (cid,))
            conn.execute("DELETE FROM posting_contacts WHERE contact_user_id=?", (user_id,))
            deleted = conn.execute("DELETE FROM employer_contacts WHERE contact_user_id=?",
                                   (user_id,)).rowcount
            conn.commit()
        return deleted


class ErmStore(_Store):
    def org_engagement(self, school_id: int) -> list[dict]:
        """C5 coordinator rollup: how engaged is each linked employer. Pure org-level counts — no
        student identity appears, so this is an operations view, not analytics about people."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT o.id AS org_id, o.name, l.status AS link_status, "
                "(SELECT COUNT(*) FROM postings p WHERE p.org_id=o.id AND p.school_id=? "
                "   AND p.status='live') AS postings_live, "
                "(SELECT COUNT(*) FROM postings p WHERE p.org_id=o.id "
                "   AND p.school_id=?) AS postings_total, "
                "(SELECT COUNT(*) FROM applications a JOIN postings p ON p.id=a.posting_id "
                "   WHERE p.org_id=o.id AND p.school_id=?) AS applications, "
                "(SELECT COUNT(*) FROM applications a JOIN postings p ON p.id=a.posting_id "
                "   WHERE p.org_id=o.id AND p.school_id=? AND a.status='hired') AS hires, "
                "(SELECT COUNT(*) FROM event_registrations r JOIN users u ON u.id=r.user_id "
                "   JOIN campus_events ev ON ev.id=r.event_id "
                "   WHERE u.org_id=o.id AND r.role='employer' AND r.status='registered' "
                "   AND ev.school_id=?) AS events_attended, "
                "(SELECT MAX(p.updated_at) FROM postings p WHERE p.org_id=o.id "
                "   AND p.school_id=?) AS last_activity "
                "FROM orgs o JOIN employer_school_links l ON l.org_id=o.id "
                "WHERE l.school_id=? AND l.status IN ('approved','pending') ORDER BY o.name",
                (school_id,) * 7).fetchall()
        return [dict(r) for r in rows]
