"""Phase-5 API surface (docs/PHASE5.md §3.2/§3.3) — mounted by create_app() next to the platform
router, only when RM_PLATFORM_ENABLED=1. Every route authenticates PER USER via require_role.

Why a second router (D7): platform.py is already 1 300+ lines of Phase-1..4 surface; the Phase-5
routes are additive and touch none of it, so they live here and the A-item enforcement edits stay
in place over there.

The boundaries this module is responsible for holding — each one is a named adversarial finding:
  * **school_id comes from `user["school_id"]`, NEVER from the body/query, and a cross-tenant id
    answers 404, never 403** (D13/SC-C1/SC-C2). The stores enforce `AND school_id=?` themselves;
    the route-level checks here exist to pick the right STATUS CODE, not to be the invariant.
  * **The mentorship decline is silent to the student AND invisible to coordinators** (D8/P-F9):
    coordinator create-offer answers a flat 202 and there is no per-offer status surface anywhere
    below `/api/coordinator/`; the only telemetry is `mentorship_stats`' MIN_CELL'd aggregates.
  * **No grad_year is collected, returned, or logged** (P-F2) — not in the alumni claim, not in the
    verification queue, not in the C1 roster. The under-networked roster's trigger is purely
    structural (zero shareable edges) and every read of it is access-logged (P-F7).
  * **`claim_role` is display-only** (D14/P-F4): nothing here branches on it.
  * **`DELETE /api/account` re-verifies the password** (SL-L3) — an irreversible cross-plane erasure
    behind a stolen session cookie is not acceptable, so the account's own PBKDF2 hash is the gate.
  * Free text that this router accepts and the shared `/api/vouches` chokepoint does NOT cover
    (the invite-submit body) is `redact_text()`-ed HERE, before it reaches create_vouch (SM-M6).

`_client_key` + `_Rate` are deliberately duplicated 4-liners rather than imported from `.app`:
app.py builds the whole demo module graph at import (and calls create_app() at the bottom), so
importing it from a router that create_app() itself imports would be circular (FL-L5).
"""
import hashlib
import logging
import threading
import time
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import env_int
from ..inference.redaction import redact_text
from ..stores.db import connect as db_connect
from ..stores.engage import EngageError, EventStore
from ..stores.erasure import ErasureError, erase_account
from ..stores.intros import IntroStore
from ..stores.notifications import NotificationStore
from ..stores.phase5 import (
    AffiliationStore,
    ContactStore,
    ErmStore,
    MentorStore,
    Phase5Error,
    VouchInviteStore,
)
from ..stores.platform import PostingStore
from ..stores.relationships import RelationshipError, RelationshipStore
from ..stores.students import StudentError, StudentStore
from ..workers.runner import get_job_store, register_handler
from .auth import require_role
from .platform import _require_own_org_posting

_log = logging.getLogger("resume_matcher.api.phase5")

router = APIRouter()

# Local by design: importing audit_store here would link the two planes at module scope, and the
# mentor_match handler below must be provably free of the audit plane (§3.3 CI-grep test). The
# audit DB is imported INSIDE the one report route that legitimately needs it.
MIN_CELL = 5

# Mentor standing (C4): a VERIFIED alum (self-claimed is not enough) or staff/employer. This is an
# attribute check, not a role check — alumni keep role='student' (D4), so no require_role changes.
_MENTOR_ROLES = ("employer", "coordinator", "admin")


def _school(user: dict) -> int:
    """The ONLY source of school_id for every route here (D13)."""
    return user.get("school_id") or 1


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_id(value) -> int | None:
    """A body field that is either an id or absent. Never let a stray 0/'' become user 0 — that is
    the erasure sentinel (§6 step 26)."""
    return _int(value, 0) or None


def _client_key(request: Request) -> str:
    """First X-Forwarded-For hop (we sit behind Caddy) else the socket peer. Trusts the header, so
    it is a convenience limiter only — the store-side caps are the real backstop."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class _Rate:
    """Process-local token bucket, same shape as app.py's — see the module docstring for why it is
    duplicated instead of imported."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(max(1, capacity))
        self.refill = max(0.001, refill_per_sec)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            if len(self._buckets) > 10000:
                self._buckets.clear()
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


# SM-M3: the check-in code is a short secret shouted across a room — guessable by design. It is only
# the SECOND factor on top of the required registration, so both the caller and their IP are capped.
_checkin_rate = _Rate(5, 5 / 600)


def _notify(user_id: int, school_id: int, kind: str, title: str, body: str = "",
            entity: str | None = None, entity_id: str | None = None) -> None:
    """Best-effort fan-out (same contract as platform.py's _notify_creator): a notification failure
    must never fail the action that earned it."""
    try:
        NotificationStore().notify(user_id, school_id, kind, title, body,
                                   entity=entity, entity_id=entity_id)
    except Exception:  # noqa: BLE001
        _log.warning("notification failed", exc_info=True)


def _enqueue_build_edges(school_id: int) -> None:
    get_job_store().enqueue("build_edges", {"school_id": school_id},
                            dedupe_key=f"build_edges:{school_id}")


# ---- notifications (B4-lite) -----------------------------------------------------------------------
@router.get("/api/notifications")
def notification_feed(unread: int = 0, page: int = 1, user: dict = Depends(require_role())):
    return NotificationStore().feed(user["id"], unread_only=bool(unread), page=max(1, page))


@router.post("/api/notifications/read")
def notifications_mark_read(body: dict, user: dict = Depends(require_role())):
    ids = body.get("ids")
    if body.get("all") or ids is None:
        return {"marked": NotificationStore().mark_read(user["id"])}
    if not isinstance(ids, list):
        raise HTTPException(400, "ids must be a list.")
    try:
        wanted = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise HTTPException(400, "ids must be integers.")
    # the store's WHERE user_id=? makes a hostile ids list a no-op, not a cross-user write (SL-L1)
    return {"marked": NotificationStore().mark_read(user["id"], wanted)}


# ---- C4: mentorship --------------------------------------------------------------------------------
def _require_mentor_standing(user: dict) -> None:
    if user.get("role") not in _MENTOR_ROLES and user.get("alumni_status") != "verified":
        raise HTTPException(403, "Mentor profiles are for verified alumni, employers, and staff.")


@router.put("/api/mentorship/profile")
def put_mentor_profile(body: dict, user: dict = Depends(require_role())):
    """The profile row IS the mentorship opt-in (D1) — but matching also leans on warm_intro, so a
    profile without it would be an opt-in that can never fire. Say so instead of accepting it."""
    _require_mentor_standing(user)
    if not StudentStore().has_consent(user["id"], "warm_intro"):
        raise HTTPException(409, "Grant the 'make warm intros' consent first — mentorship "
                                 "matching runs on it.")
    try:
        return MentorStore().upsert_profile(
            user["id"], _school(user), program=str(body.get("program") or ""),
            topics=str(body.get("topics") or ""), capacity=_int(body.get("capacity"), 3),
            active=bool(body.get("active", True)))
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/mentorship/profile")
def delete_mentor_profile(user: dict = Depends(require_role())):
    """The opt-out (D1). Deliberately NOT behind _require_mentor_standing: someone whose verified
    status was withdrawn must still be able to delete the row they created."""
    return {"deleted": MentorStore().delete_profile(user["id"])}


@router.get("/api/mentorship/offers")
def mentor_offers(user: dict = Depends(require_role())):
    return {"offers": MentorStore().offers_for_mentor(user["id"])}


@router.post("/api/mentorship/offers/{offer_id}/respond")
def respond_mentor_offer(offer_id: str, body: dict, user: dict = Depends(require_role())):
    """Mentor-only. A decline notifies NOBODY and appears on no coordinator surface (D8): to the
    student it is indistinguishable from an offer nobody got round to."""
    accept = bool(body.get("accept"))
    try:
        out = MentorStore().respond_offer(offer_id, user["id"], accept)
    except Phase5Error as exc:
        # wrong mentor, unknown id, or already-terminal all read as absent: an offer you cannot act
        # on is gone, and the uniform 404 keeps a non-recipient from probing offer ids.
        raise HTTPException(404, str(exc))
    if out["status"] == "accepted":
        _notify(out["student_user_id"], out["school_id"], "mentorship_accepted",
                "A mentor accepted your match", "Open your network card to say hello.",
                entity="mentorship_offer", entity_id=offer_id)
    return {"status": out["status"]}


@router.get("/api/coordinator/mentorship-stats")
def mentorship_stats(user: dict = Depends(require_role("coordinator", "admin"))):
    """P-F9: the ONLY coordinator-visible mentorship telemetry — MIN_CELL'd aggregates. There is no
    per-offer, per-student, or per-mentor status anywhere under /api/coordinator/."""
    return MentorStore().mentorship_stats(_school(user))


# ---- C4: alumni ------------------------------------------------------------------------------------
@router.post("/api/alumni/claim")
def claim_alumni(user: dict = Depends(require_role("student"))):
    """SC-C2: the subject and the status are HARD-CODED — a student can only ever move themselves to
    'self_claimed', never to 'verified', and never touch another account. No grad_year is collected
    (P-F2): the coordinator verifies against SIS by email, out of band."""
    try:
        StudentStore().set_alumni_status(user["id"], _school(user), "self_claimed")
    except StudentError as exc:
        raise HTTPException(404, str(exc))
    return {"alumni_status": "self_claimed"}


@router.get("/api/coordinator/alumni")
def alumni_queue(user: dict = Depends(require_role("coordinator", "admin"))):
    return {"claims": StudentStore().alumni_queue(_school(user))}


@router.post("/api/coordinator/alumni/{user_id}/verify")
def verify_alumni(user_id: int, body: dict,
                  user: dict = Depends(require_role("coordinator", "admin"))):
    """The coordinator checked graduation records out of band; this click IS the attestation record
    (an append-only `events` row, written by the store). Cross-school user_id -> 404 (SC-C2)."""
    approve = bool(body.get("approve"))
    status = "verified" if approve else "none"
    try:
        StudentStore().set_alumni_status(user_id, _school(user), status,
                                         attested_by=user["id"] if approve else None)
    except StudentError as exc:
        raise HTTPException(404, str(exc))
    return {"user_id": user_id, "alumni_status": status}


# ---- C1: relationship health (structural triggers only) --------------------------------------------
def _under_networked(school_id: int) -> list[dict]:
    """Discoverable students with ZERO shareable edges who ALSO hold network_analytics consent.

    The analytics consent is required on top of graph_discoverable (FH-H3): an individual-level
    roster is analytics ABOUT a person's network position, not a by-product of being discoverable.
    The trigger is purely structural — no self-ID attribute, and no grad_year, appears anywhere in
    this query (P-F2/P-F7, test-enforced)."""
    now = time.time()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT u.id AS user_id, u.email, p.program FROM users u "
            "LEFT JOIN student_profiles p ON p.user_id=u.id "
            "WHERE u.school_id=? AND u.role='student' "
            "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=u.id "
            "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
            "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=u.id "
            "          AND c.purpose='network_analytics' AND c.revoked_at IS NULL) "
            "AND NOT EXISTS(SELECT 1 FROM graph_edges ge WHERE ge.school_id=? "
            "          AND (ge.user_a=u.id OR ge.user_b=u.id) AND ge.consent_state='shareable' "
            "          AND ge.revoked_at IS NULL "
            "          AND (ge.expires_at IS NULL OR ge.expires_at > ?)) "
            "ORDER BY u.id", (school_id, school_id, now)).fetchall()
    return [{"user_id": r["user_id"], "email": r["email"], "program": r["program"], "degree": 0}
            for r in rows]


@router.get("/api/coordinator/under-networked")
def under_networked(user: dict = Depends(require_role("coordinator", "admin"))):
    """C1. The roster of a structurally disadvantaged class is exactly the kind of list that needs a
    paper trail, so EVERY read appends an access-log row (P-F7) — the governed positive-action
    program's monitoring surface, per RELATIONSHIPS.md Slice AI."""
    school = _school(user)
    students = _under_networked(school)
    with closing(db_connect()) as conn:
        conn.execute("INSERT INTO events(actor_user_id, action, entity, entity_id, at) "
                     "VALUES(?,?,?,?,?)",
                     (user["id"], "under_networked_viewed", "school", str(school), time.time()))
        conn.commit()
    return {"students": students, "total": len(students)}


@router.get("/api/coordinator/mentors")
def coordinator_mentors(user: dict = Depends(require_role("coordinator", "admin"))):
    return {"mentors": MentorStore().eligible_mentors(_school(user))}


def _in_school(conn, user_id: int, school_id: int) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE id=? AND school_id=?",
                        (user_id, school_id)).fetchone() is not None


@router.post("/api/coordinator/mentorship-offers", status_code=202)
def coordinator_offer_mentor(body: dict,
                            user: dict = Depends(require_role("coordinator", "admin"))):
    """The double-opt-in replacement for blind edge-minting: the coordinator proposes, the MENTOR
    decides, and the coordinator never learns which way it went (D8).

    The store already refuses cross-tenant ids; this pre-check only exists to answer 404 instead of
    409 (SC-C1). The store remains the invariant."""
    student_id, mentor_id = _int(body.get("student_id"), 0), _int(body.get("mentor_id"), 0)
    if not student_id or not mentor_id:
        raise HTTPException(400, "student_id and mentor_id are required.")
    school = _school(user)
    with closing(db_connect()) as conn:
        if not _in_school(conn, student_id, school) or not _in_school(conn, mentor_id, school):
            raise HTTPException(404, "No such user.")
    try:
        out = MentorStore().create_offer(school_id=school, student_user_id=student_id,
                                         mentor_user_id=mentor_id, origin="coordinator",
                                         rationale="coordinator bridge")
    except Phase5Error as exc:
        # "open offer exists" and "recently declined" share one message on purpose: distinguishing
        # them would turn re-offering into a decline oracle (D8).
        raise HTTPException(409, str(exc))
    _notify(mentor_id, school, "mentorship_offer", "A coordinator suggested a mentorship match",
            "Open your mentor panel to accept or decline.",
            entity="mentorship_offer", entity_id=out["offer_id"])
    return {"status": "offered"}   # no offer_id: there is no status surface to poll (D8)


# ---- C2: intro outcomes (aggregate-only, snapshot-served) -------------------------------------------
def _serve_snapshot(report_key: str, school_id: int, refs_count: int, compute):
    """A8 serving policy: keep serving the PINNED payload unless the snapshot is BOTH stale and the
    cohort has moved by at least MIN_CELL. Recomputing on every request lets a coordinator diff two
    reads taken either side of one student's consent flip and subtract that student back out."""
    from ..stores.audit_store import AuditDB

    audit = AuditDB()
    snap = audit.get_snapshot(report_key, school_id)
    max_age = max(1, env_int("RM_AUDIT_SNAPSHOT_HOURS", 24)) * 3600
    if snap is not None:
        stale = (time.time() - snap["computed_at"]) > max_age
        moved = abs(refs_count - snap["refs_count"]) >= MIN_CELL
        if not (stale and moved):
            return snap["payload"]
    payload = compute()
    audit.save_snapshot(report_key, school_id, payload, refs_count)
    return payload


_OUTCOME_STAGES = ("accepted", "shortlisted", "hired")
# An application that reached shortlisted/advanced/hired cleared the shortlist stage; hired is the
# terminal one. Counted off the application the intro was attached to (intros.outcome_rows).
_SHORTLISTED_STATES = ("shortlisted", "advanced", "hired")


def _outcome_counts(rows: list[dict], key) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(key(row), {"requested": 0, "accepted": 0, "shortlisted": 0,
                                           "hired": 0})
        bucket["requested"] += 1
        if row["status"] == "accepted":
            bucket["accepted"] += 1
        app_status = row.get("application_status")
        if app_status in _SHORTLISTED_STATES:
            bucket["shortlisted"] += 1
        if app_status == "hired":
            bucket["hired"] += 1
    return out


@router.get("/api/coordinator/reports/intro-outcomes")
def intro_outcomes(format: str = "json",
                   user: dict = Depends(require_role("coordinator", "admin"))):
    """C2: does a BRIDGED intro convert like an organic one? Aggregate-only, every cell MIN_CELL'd,
    cohort filtered to students who granted network_analytics (A7), served through the A8 snapshot
    policy so the report can't be differenced down to one student."""
    from ..audit.metrics import origin_impact

    school = _school(user)
    rows = IntroStore().outcome_rows(school)
    requesters = sorted({r["requester_user_id"] for r in rows})
    consenting = set(StudentStore().filter_by_consent(requesters, "network_analytics"))
    rows = [r for r in rows if r["requester_user_id"] in consenting]

    def _compute() -> dict:
        by_kind = {}
        for kind, counts in _outcome_counts(rows, lambda r: r["broker_edge_kind"] or "unknown").items():
            by_kind[kind] = {stage: (n if n >= MIN_CELL else None)
                             for stage, n in counts.items()}
        return {"by_origin": origin_impact(_outcome_counts(rows, lambda r: r["origin"]), MIN_CELL),
                "by_broker_kind": by_kind, "min_cell": MIN_CELL}

    payload = _serve_snapshot("intro_outcomes", school, len(consenting), _compute)
    if format == "csv":
        import csv
        import io

        from fastapi.responses import PlainTextResponse

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["origin", "stage", "n", "rate", "bridged_over_organic"])
        by_origin = payload["by_origin"]
        for origin in ("organic", "bridged"):
            for stage in _OUTCOME_STAGES:
                cell = by_origin["by_origin"][origin]["stages"][stage]
                ratio = by_origin["bridged_over_organic"][stage].get("ratio")
                # a suppressed cell is None from origin_impact and renders as an EMPTY field here —
                # never a count, and never a 0 (a hidden cell is not the same as no one)
                w.writerow([origin, stage, cell["n"] if cell["n"] is not None else "",
                            cell["rate"] if cell["rate"] is not None else "",
                            ratio if origin == "bridged" and ratio is not None else ""])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return payload


# ---- C3: event check-ins ---------------------------------------------------------------------------
@router.post("/api/events/{event_id}/checkin-code")
def event_checkin_code(event_id: str, user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return {"code": EventStore().set_checkin_code(event_id, _school(user))}
    except EngageError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/events/{event_id}/checkin")
def event_checkin(event_id: str, body: dict, request: Request,
                  user: dict = Depends(require_role("student", "employer"))):
    """Self check-in. SM-M3: capped per CALLER and per IP — the code is low-entropy and shouted at a
    fair, so it only ever works as a second factor on top of the caller's own registration."""
    now = time.time()
    if not _checkin_rate.allow(f"u:{user['id']}", now) or not _checkin_rate.allow(
            f"ip:{_client_key(request)}", now):
        raise HTTPException(429, "Too many check-in attempts — wait a moment and try again.")
    try:
        EventStore().checkin_by_code(event_id, user["id"], str(body.get("code") or ""))
    except EngageError as exc:
        raise HTTPException(409, str(exc))
    _enqueue_build_edges(_school(user))
    return {"ok": True}


@router.post("/api/coordinator/events/{event_id}/checkins")
def roster_checkin(event_id: str, body: dict,
                   user: dict = Depends(require_role("coordinator", "admin"))):
    school = _school(user)
    try:
        EventStore().checkin_roster(event_id, _int(body.get("user_id"), 0), user["id"], school)
    except EngageError as exc:
        raise HTTPException(404, str(exc))   # wrong school / wrong tenant reads as absent (D13)
    _enqueue_build_edges(school)
    return {"ok": True}


@router.get("/api/events/{event_id}/checkins")
def event_checkins(event_id: str, user: dict = Depends(require_role("coordinator", "admin"))):
    return {"checkins": EventStore().checkins(event_id, _school(user))}


# ---- C6: affiliations ------------------------------------------------------------------------------
@router.post("/api/affiliations/claim", status_code=201)
def affiliation_claim(body: dict, user: dict = Depends(require_role("student"))):
    """claim_role rides along for DISPLAY only (D14) — nothing here or downstream branches on it."""
    try:
        return AffiliationStore().claim(
            user_id=user["id"], school_id=_school(user), kind=str(body.get("kind") or ""),
            label=str(body.get("label") or ""), term=str(body.get("term") or ""),
            claim_role=str(body.get("claim_role") or "member"))
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/affiliations/mine")
def affiliation_mine(user: dict = Depends(require_role())):
    return {"claims": AffiliationStore().mine(user["id"])}


@router.get("/api/affiliations/{affiliation_id}/claimants")
def affiliation_claimants(affiliation_id: str, user: dict = Depends(require_role())):
    """P-F1 hard requirement: a CONFIRMED claim is the price of admission, confirmed co-claimants
    only, emails masked outside an attestation pair. Anything less is an enumeration oracle."""
    try:
        return {"claimants": AffiliationStore().claimants(affiliation_id, user["id"])}
    except Phase5Error as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/affiliations/claims/{claim_id}/confirm")
def affiliation_confirm(claim_id: str, user: dict = Depends(require_role())):
    """The confirm-link target — claim_id IS the capability (D15). Every failure here is
    client-correctable and names nothing the caller doesn't already hold, so they share one 400."""
    try:
        out = AffiliationStore().confirm(claim_id, user["id"])
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))
    if out["status"] == "confirmed":
        with closing(db_connect()) as conn:
            row = conn.execute(
                "SELECT c.user_id, af.school_id, af.label_display FROM affiliation_claims c "
                "JOIN affiliations af ON af.id=c.affiliation_id WHERE c.id=?",
                (claim_id,)).fetchone()
        if row is not None:
            # only the claim owner hears about it: the draft's broadcast to co-claimants was an
            # F1 aggravator and is removed (§2.11)
            _notify(row["user_id"], row["school_id"], "affiliation_confirmed",
                    "A classmate confirmed your claim", str(row["label_display"] or ""),
                    entity="affiliation", entity_id=claim_id)
    return out


@router.delete("/api/affiliations/claims/{claim_id}")
def affiliation_remove(claim_id: str, user: dict = Depends(require_role())):
    return {"deleted": AffiliationStore().remove_claim(claim_id, user["id"])}


# ---- C7: vouch invites -----------------------------------------------------------------------------
def _invite_handle(token: str) -> str:
    """A non-secret handle for the invite, for the coordinator queue's display only. The token is a
    live capability, so it never travels anywhere but back to its owner (D10/FL-L2)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


@router.post("/api/vouches/invites", status_code=201)
def create_vouch_invite(body: dict, user: dict = Depends(require_role())):
    """C7/D10: invite-by-link, never member search — there is no member-search endpoint anywhere in
    Phase 5. The token is returned ONCE, inside the URL; only its sha256 is at rest."""
    hint = body.get("relationship_hint")
    try:
        out = VouchInviteStore().create(subject_user_id=user["id"], school_id=_school(user),
                                        relationship_hint=str(hint) if hint else None)
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))
    return {"invite_url": out["invite_url"], "expires_at": out["expires_at"]}


@router.get("/api/vouches/invites")
def my_vouch_invites(user: dict = Depends(require_role())):
    """The subject's open asks. No tokens: the subject already copied the link, and re-serving a
    live capability from a list view is how capabilities leak."""
    return {"invites": VouchInviteStore().open_for_subject(user["id"])}


@router.get("/api/vouches/invites/{token}")
def read_vouch_invite(token: str, user: dict = Depends(require_role())):
    """The signed-in holder of the link sees who asked. Cross-school is absent, not forbidden
    (SM-M1): the vouch always lands in the INVITE's school, so a foreign holder has no next step."""
    invite = VouchInviteStore().get_open(token, _school(user))
    if invite is None:
        raise HTTPException(404, "That invite link is no longer valid.")
    return {"subject_email": invite["subject_email"],
            "relationship_hint": invite["relationship_hint"],
            "expires_at": invite["expires_at"]}


@router.post("/api/vouches/invites/{token}/submit", status_code=201)
def submit_vouch_invite(token: str, body: dict, user: dict = Depends(require_role())):
    """SM-M6: this route does NOT share /api/vouches' ingest chokepoint, so `relationship` and
    `evidence` are redacted HERE, before create_vouch ever sees them.

    D14: no affiliation, invite, or claim_role produces a verification tier — every invited vouch
    starts at 'self' and only a coordinator can lift it."""
    school = _school(user)
    invite = VouchInviteStore().get_open(token, school)
    if invite is None:
        raise HTTPException(404, "That invite link is no longer valid.")
    relationship = redact_text(str(body.get("relationship") or "")).strip() or None
    evidence = redact_text(str(body.get("evidence") or ""))
    try:
        vouch = RelationshipStore().create_vouch(
            school_id=invite["school_id"], voucher_user_id=user["id"],
            subject_user_id=invite["subject_user_id"], relationship=relationship,
            evidence=evidence, verify_level="self", via_invite_id=_invite_handle(token))
    except RelationshipError as exc:
        raise HTTPException(400, str(exc))
    try:
        VouchInviteStore().consume(token, user["id"], school, vouch["vouch_id"])
    except Phase5Error as exc:
        raise HTTPException(404, str(exc))
    _notify(invite["subject_user_id"], invite["school_id"], "vouch_received",
            "Someone answered your reference request",
            "Open your reference ledger to read it.", entity="vouch",
            entity_id=vouch["vouch_id"])
    return {"vouch_id": vouch["vouch_id"]}


@router.delete("/api/vouches/invites/{token}")
def revoke_vouch_invite(token: str, user: dict = Depends(require_role())):
    return {"revoked": VouchInviteStore().revoke(token, user["id"])}


# ---- B10: coordinator vouch queue ------------------------------------------------------------------
@router.get("/api/coordinator/vouches")
def coordinator_vouches(status: str = "contested",
                        user: dict = Depends(require_role("coordinator", "admin"))):
    try:
        return {"vouches": RelationshipStore().vouches_for_coordinator(_school(user), status)}
    except RelationshipError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/vouches/{vouch_id}/resolve")
def resolve_vouch(vouch_id: str, body: dict,
                  user: dict = Depends(require_role("coordinator", "admin"))):
    """B10 dead-end fix: the queue finally has actions. School-scoped — a vouch in another tenant is
    absent, so this answers 404 rather than 403 (SC-C1)."""
    action = str(body.get("action") or "")
    if action not in ("verify", "dismiss"):
        raise HTTPException(400, "action must be 'verify' or 'dismiss'.")
    try:
        return RelationshipStore().resolve_vouch(_school(user), vouch_id, user["id"], action)
    except RelationshipError as exc:
        raise HTTPException(404, str(exc))


# ---- C5: employer contacts + posting contact + ERM -------------------------------------------------
def _require_org(user: dict) -> int:
    if user.get("org_id") is None:
        raise HTTPException(409, "Your account isn't linked to an organization yet.")
    return user["org_id"]


@router.post("/api/orgs/me/contacts", status_code=201)
def add_org_contact(body: dict, user: dict = Depends(require_role("employer"))):
    """PIPEDA business-contact exemption: an employer naming its OWN people. The store caps,
    redacts, escapes and CSV-neutralizes the free text at write (P-F5) and refuses a
    contact_user_id that isn't an own-org member (SM-M4)."""
    try:
        return ContactStore().add_contact(
            org_id=_require_org(user), school_id=_school(user), added_by=user["id"],
            display_label=str(body.get("display_label") or ""),
            role_title=str(body.get("role_title") or ""),
            contact_user_id=_opt_id(body.get("contact_user_id")))
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/orgs/me/contacts")
def list_org_contacts(user: dict = Depends(require_role("employer"))):
    return {"contacts": ContactStore().list_contacts(_require_org(user))}


@router.delete("/api/orgs/me/contacts/{contact_id}")
def delete_org_contact(contact_id: str, user: dict = Depends(require_role("employer"))):
    """THE C5 deletion path: the contact AND every posting_contacts row pointing at it."""
    return {"deleted": ContactStore().delete_contact(contact_id, _require_org(user))}


@router.put("/api/postings/{posting_id}/contact")
def set_posting_contact(posting_id: str, body: dict,
                        user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    """Names the real hiring manager so the pathfinder aims at a human instead of whoever pasted
    the JD. The own-org membership check (SM-M4) is what stops a posting from pointing the graph at
    an arbitrary account."""
    posting = PostingStore().get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    try:
        return ContactStore().set_posting_contact(
            posting_id=posting_id, school_id=posting["school_id"], added_by=user["id"],
            contact_user_id=_opt_id(body.get("contact_user_id")),
            employer_contact_id=body.get("employer_contact_id") or None,
            relation=str(body.get("relation") or "hiring_manager"))
    except Phase5Error as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/postings/{posting_id}/contact")
def clear_posting_contact(posting_id: str,
                          user: dict = Depends(require_role("employer", "coordinator", "admin"))):
    posting = PostingStore().get(posting_id)
    if posting is None:
        raise HTTPException(404, "No such posting.")
    _require_own_org_posting(posting, user)
    return {"deleted": ContactStore().clear_posting_contact(posting_id)}


@router.get("/api/coordinator/orgs")
def coordinator_orgs(user: dict = Depends(require_role("coordinator", "admin"))):
    """C5 ERM rollup: org-level counts only — no student identity crosses this surface."""
    return {"orgs": ErmStore().org_engagement(_school(user))}


# ---- A3: self-serve erasure ------------------------------------------------------------------------
@router.delete("/api/account")
def erase_my_account(body: dict, user: dict = Depends(require_role())):
    """SL-L3: irreversible, cross-plane, and reachable from any tab holding the session cookie — so
    it takes a step-up re-auth. `confirm_email` is the are-you-sure; the PASSWORD is the gate.

    Employers: the org's business records (postings) survive with an erased-user sentinel; §6."""
    from .accounts import AccountError, get_account_store

    if str(body.get("confirm_email") or "").strip().lower() != (user["email"] or "").lower():
        raise HTTPException(400, "Type your account email exactly to confirm.")
    try:
        # re-verify against the account's own PBKDF2 hash; the token this mints dies in the
        # cascade's phase 0 (DELETE FROM tokens WHERE user_id=?) moments later
        get_account_store().login(user["email"], str(body.get("password") or ""))
    except AccountError:
        raise HTTPException(403, "Wrong password.")
    try:
        out = erase_account(user["id"], reason="member_deleted")
    except ErasureError as exc:
        raise HTTPException(404, str(exc))
    return {"erased": True, "tables": out["tables"]}


# ---- §3.3: the mentorship matcher job --------------------------------------------------------------
def _program_tokens(program: str | None) -> set[str]:
    return {t for t in (program or "").lower().replace("/", " ").replace("-", " ").split()
            if len(t) > 2}


@router.post("/api/coordinator/mentor-match", status_code=202)
def run_mentor_match(user: dict = Depends(require_role("coordinator", "admin"))):
    """The coordinator button behind the C4 matcher (§3.3). 202 + poll, like every other platform
    job; the result is a count, never a per-offer status (D8)."""
    school = _school(user)
    job_id = get_job_store().enqueue("mentor_match", {"school_id": school},
                                     owner_user_id=user["id"],
                                     dedupe_key=f"mentor_match:{school}")
    return {"job_id": job_id, "poll": f"/api/jobs/{job_id}"}


@register_handler("mentor_match")
def _mentor_match_job(payload: dict, progress) -> dict:
    """C4 matcher: offer each under-networked student the best-fitting mentor with room.

    STRUCTURAL TRIGGERS ONLY — the cohort predicate is 'zero shareable edges' (+ the two consents),
    identical to C1's; this handler never touches the audit plane and imports neither audit_store
    nor data_planes (§3.3 CI-grep test). Ranking is program-token overlap then least-loaded, and
    the rationale is a string of structural facts — no fabricated probability anywhere.

    At most ONE open offer per student; the store's pair cooldown does the rest, so a decline can
    never be probed by re-running this job."""
    school = int(payload["school_id"])
    mentors = MentorStore().eligible_mentors(school)
    students = _under_networked(school)
    progress(0, len(students))
    if not mentors:
        return {"offers": 0, "students": len(students), "mentors": 0}
    load: dict[int, int] = {}
    made = 0
    for i, student in enumerate(students):
        with closing(db_connect()) as conn:
            open_offer = conn.execute(
                "SELECT 1 FROM mentorship_offers WHERE student_user_id=? AND status='offered'",
                (student["user_id"],)).fetchone()
        if open_offer:
            progress(i + 1)
            continue
        wanted = _program_tokens(student.get("program"))
        ranked = sorted(
            mentors,
            key=lambda m: (-len(wanted & _program_tokens(m.get("program"))),
                           load.get(m["user_id"], 0), -(m.get("capacity_left") or 0)))
        for mentor in ranked:
            if load.get(mentor["user_id"], 0) >= (mentor.get("capacity_left") or 0):
                continue
            shared = sorted(wanted & _program_tokens(mentor.get("program")))
            rationale = (f"program overlap: {', '.join(shared)}" if shared
                         else "matched on mentor availability (no program overlap)")
            try:
                out = MentorStore().create_offer(
                    school_id=school, student_user_id=student["user_id"],
                    mentor_user_id=mentor["user_id"], origin="matcher", rationale=rationale)
            except Phase5Error:
                continue   # open offer or cooldown for this pair — try the next mentor
            load[mentor["user_id"]] = load.get(mentor["user_id"], 0) + 1
            made += 1
            _notify(mentor["user_id"], school, "mentorship_offer",
                    "A student was matched to you for mentorship", rationale,
                    entity="mentorship_offer", entity_id=out["offer_id"])
            break
        progress(i + 1)
    return {"offers": made, "students": len(students), "mentors": len(mentors)}
