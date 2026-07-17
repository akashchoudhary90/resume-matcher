"""Account erasure (Phase 5 A3 — docs/PHASE5.md §6): the ONE cascade behind both
`DELETE /api/account` (self-serve) and `scripts/dsr_erase.py` (operator DSR).

Ordering is load-bearing (D12). Cross-DB atomicity between platform.db and audit.db is impossible
— two SQLite files, never one connection (boundary #2 forbids even trying) — so the order is
chosen so that EVERY failure mode is retry-safe rather than atomic:

  Phase 0  platform.db, autocommit: DELETE the user's tokens. The session dies FIRST, so nothing
           the account holder does can race the rest of the cascade.
  Phase 1  audit.db: drop the self-ID row. On failure we abort with the account otherwise intact
           and the caller retries. (Doing it after the platform delete would strand a self-ID row
           whose candidate_ref no longer resolves to anybody — an orphan we could never find.)
  Phase 2  platform.db, ONE `BEGIN IMMEDIATE`: table-by-table, tombstone, then the users row, then
           COMMIT. A crash mid-transaction rolls back to the phase-0/1 state; re-running finishes.

Two invariants the table plan encodes:
  * People data is HARD-deleted; append-only logs (events / posting_events / intro_events) are
    ANONYMIZED, never deleted — the audit-retention basis survives the person (privacy F8 also
    nulls the erased uid where they are the SUBJECT of a logged action, not just the actor).
  * The suppression tombstone is INSERTed inside the same transaction as the deletes, so no
    partially-visible state can ever re-materialize an edge for the erased member.

`NetworkStore.delete_my_network()` is deliberately NOT called as a sub-step (it opens its own
connection/transaction): its statements are inlined below so the platform plane commits once.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing

from .audit_store import AuditDB
from .db import connect, migrate, platform_db_path

# postings are ORG records, not personal data, so they outlive their author; created_by is
# re-pointed at this sentinel. Every edge fold carries `AND p.created_by != 0` and
# `_hiring_manager` maps 0 -> None, so the sentinel is never treated as a person (feasibility M5).
ERASED_USER_SENTINEL = 0


class ErasureError(Exception):
    """The account cannot be erased (unknown user, or the audit plane refused) -> HTTP 400/500."""


def _plan(uid: int) -> list[tuple[str, str, str, tuple]]:
    """(table, kind, where, params) for every step of §6's phase-2 order, children before parents.

    kind 'delete' hard-deletes people data; kind 'update:<SET clause>' anonymizes a reference that
    must survive (append-only logs, business records, a peer's own row). Both kinds are counted the
    same way in a dry run (`SELECT COUNT(*) ... WHERE <where>`), so the preview never lies about an
    anonymization it would perform.
    """
    apps = "SELECT id FROM applications WHERE student_id=?"
    return [
        ("notifications", "delete", "user_id=?", (uid,)),
        ("intro_requests", "delete",
         "requester_user_id=? OR target_user_id=? OR broker_user_id=?", (uid, uid, uid)),
        ("vouches", "delete", "voucher_user_id=? OR subject_user_id=?", (uid, uid)),
        ("vouch_invites", "delete", "subject_user_id=?", (uid,)),
        ("vouch_invites", "update:used_by=NULL", "used_by=?", (uid,)),
        ("graph_edges", "delete", "user_a=? OR user_b=?", (uid, uid)),
        ("member_graph_identity", "delete", "user_id=?", (uid,)),
        ("broker_blocks", "delete", "broker_user_id=? OR blocked_user_id=?", (uid, uid)),
        ("mentor_profiles", "delete", "user_id=?", (uid,)),
        ("mentorship_offers", "delete", "student_user_id=? OR mentor_user_id=?", (uid, uid)),
        ("affiliation_claims", "delete", "user_id=?", (uid,)),
        # the peer's OWN claim survives; only the attestation link is anonymized, and the C6 fold
        # requires confirmed_by IS NOT NULL, so it can never re-mint an edge to a deleted uid.
        ("affiliation_claims", "update:confirmed_by=NULL", "confirmed_by=?", (uid,)),
        ("event_checkins", "delete", "user_id=?", (uid,)),
        ("event_registrations", "delete", "user_id=?", (uid,)),
        # messages/interview_slots reach through applications, so they precede the applications row
        ("messages", "delete", f"sender_user_id=? OR application_id IN ({apps})", (uid, uid)),
        ("interview_slots", "delete", f"application_id IN ({apps}) OR proposed_by=?", (uid, uid)),
        ("applications", "delete", "student_id=?", (uid,)),
        ("match_results", "delete", "student_id=?", (uid,)),
        ("resumes", "delete", "user_id=?", (uid,)),          # blob + text, hard
        ("student_profiles", "delete", "user_id=?", (uid,)),
        ("projects", "delete", "user_id=?", (uid,)),
        ("posting_contacts", "delete", "contact_user_id=?", (uid,)),
        ("posting_contacts", f"update:added_by={ERASED_USER_SENTINEL}", "added_by=?", (uid,)),
        ("employer_contacts", "delete", "contact_user_id=?", (uid,)),   # C5 erasure hook
        ("employer_contacts", f"update:added_by={ERASED_USER_SENTINEL}", "added_by=?", (uid,)),
        ("repudiation_requests", "update:decided_by=NULL", "decided_by=?", (uid,)),
        ("jobs", "delete", "owner_user_id=? AND status IN ('queued','running')", (uid,)),
        ("jobs", "update:payload_json='{}', result_json=NULL, owner_user_id=NULL",
         "owner_user_id=?", (uid,)),
        # POLICY (PRIVACY.md): the tombstone + the anonymized events row are the durable erasure
        # proof; keeping per-purpose consent history for a deleted person is itself retained PII.
        ("consents", "delete", "user_id=?", (uid,)),
        # append-only logs — anonymize, never delete (audit-retention basis)
        ("events", "update:actor_user_id=NULL", "actor_user_id=?", (uid,)),
        ("events", "update:entity_id=NULL", "entity='user' AND entity_id=CAST(? AS TEXT)", (uid,)),
        ("posting_events", "update:actor_user_id=NULL", "actor_user_id=?", (uid,)),
        ("intro_events", "update:actor_user_id=NULL", "actor_user_id=?", (uid,)),
        # employer business records survive the person; the author becomes the sentinel and any
        # review decision they made is anonymized (§6 step 26).
        ("postings", f"update:created_by={ERASED_USER_SENTINEL}", "created_by=?", (uid,)),
        ("postings", "update:reviewed_by=NULL", "reviewed_by=?", (uid,)),
        ("employer_school_links", "update:reviewed_by=NULL", "reviewed_by=?", (uid,)),
    ]


def _orphan_path_intros(conn, uid: int) -> list[str]:
    """privacy F8: an intro whose ranked path merely PASSES THROUGH the erased member is not
    covered by the three principal columns (a >2-hop path has interior nodes). The request is
    meaningless without its full path, so it goes too. Scanned in Python — path_json is opaque."""
    doomed: list[str] = []
    # Rows the principal-column DELETE already covers are excluded here, so the count is honest in
    # a dry run too (where that DELETE has not happened yet and would otherwise be double-counted).
    for row in conn.execute(
        "SELECT id, path_json FROM intro_requests "
        "WHERE requester_user_id!=? AND target_user_id!=? AND broker_user_id!=?", (uid, uid, uid)
    ):
        try:
            nodes = json.loads(row["path_json"] or "{}").get("nodes") or []
        except (ValueError, TypeError):
            continue  # unparseable path: the principal-column delete already covered the row
        if uid in nodes:
            doomed.append(row["id"])
    return doomed


def _close_orphaned_live_postings(conn, uid: int, org_id: int | None) -> int:
    """§6 step 26: postings survive their author, but an org whose LAST member is erased must not
    keep live postings nobody can answer. Runs BEFORE created_by is re-pointed at the sentinel."""
    if org_id is None:
        return 0
    others = conn.execute(
        "SELECT COUNT(*) FROM users WHERE org_id=? AND id!=?", (org_id, uid)).fetchone()[0]
    if others:
        return 0
    return conn.execute(
        "UPDATE postings SET status='closed', updated_at=? WHERE org_id=? AND status='live'",
        (time.time(), org_id)).rowcount


def erase_account(user_id: int, *, reason: str = "member_deleted",
                  dry_run: bool = False) -> dict:
    """Erase one account across both planes. Idempotent at every step: a crash anywhere is fixed
    by re-running. `dry_run=True` writes NOTHING and returns the would-delete counts."""
    uid = int(user_id)
    path = platform_db_path()
    migrate(path)

    with closing(connect(path)) as conn:
        row = conn.execute("SELECT school_id, org_id FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise ErasureError(f"No account with id {uid}.")
        school_id, org_id = row["school_id"], row["org_id"]

        if dry_run:
            tables: dict[str, int] = {"tokens": conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE user_id=?", (uid,)).fetchone()[0]}
            for table, _kind, where, params in _plan(uid):
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
                tables[table] = tables.get(table, 0) + n
            tables["intro_requests"] += len(_orphan_path_intros(conn, uid))
            tables["users"] = 1
            return {"dry_run": True, "tables": tables, "tombstoned": False,
                    "audit_plane_deleted": False}

    # --- phase 0: kill the session first, so nothing races the cascade (autocommit) --------------
    with closing(connect(path)) as conn:
        tokens_killed = conn.execute("DELETE FROM tokens WHERE user_id=?", (uid,)).rowcount
        conn.commit()

    # --- phase 1: audit plane BEFORE the platform plane (an orphan self-ID row is unfindable) ----
    audit_deleted = AuditDB().delete_self_id(f"student-{uid}")

    # --- phase 2: the whole platform plane in ONE transaction ------------------------------------
    tables: dict[str, int] = {"tokens": tokens_killed}
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _close_orphaned_live_postings(conn, uid, org_id)
            for table, kind, where, params in _plan(uid):
                if kind == "delete":
                    sql = f"DELETE FROM {table} WHERE {where}"
                else:
                    sql = f"UPDATE {table} SET {kind.split(':', 1)[1]} WHERE {where}"
                n = conn.execute(sql, params).rowcount
                tables[table] = tables.get(table, 0) + n
            orphans = _orphan_path_intros(conn, uid)
            if orphans:
                marks = ",".join("?" * len(orphans))
                tables["intro_requests"] += conn.execute(
                    f"DELETE FROM intro_requests WHERE id IN ({marks})", orphans).rowcount
            # the tombstone rides INSIDE the transaction: no visible window in which the member is
            # gone but un-suppressed (an edge fold could otherwise re-materialize them).
            conn.execute(
                "INSERT INTO graph_suppressions(school_id, user_id, reason, created_at) "
                "VALUES(?,?,?,?)", (school_id, uid, reason, time.time()))
            tables["users"] = conn.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"dry_run": False, "tables": tables, "tombstoned": True,
            "audit_plane_deleted": audit_deleted}


def user_id_hash(email: str) -> str:
    """Non-reversible handle for the DSR receipt: the file must prove WHICH request was executed
    without re-storing the erased person's address in the operator's paperwork."""
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()[:16]
