"""Relationship-graph retention/erasure job (docs/RELATIONSHIPS.md Slice AK + PHASE5.md §2.15).

Enforces the retention schedule so PII does not persist indefinitely (a FIPPA/PIPEDA obligation
the adversarial review flagged):
  * self-upload / derived graph_edges are hard-deleted once past their expires_at (default 12mo);
  * intro_requests are hard-deleted once past purge_after (set on a terminal status, +6mo);
  * requested intros past their expiry are swept to 'expired' first (so they then get a purge_after).

Phase-5 additions:
  * notifications past their read/unread windows are purged (NotificationStore.purge);
  * undecided repudiation rows past expires_at are expired AND scrubbed — the asserted
    first/last/company (and challenge hashes) never persist past the TTL (privacy F6);
    decided/expired rows are hard-deleted once past purge_after;
  * expired admin_sessions rows are deleted (security M5 — lazy purge alone leaves
    never-revisited rows);
  * mentorship offers and vouch invites are swept to 'expired' (stores/phase5.py).

intro_events carry only opaque IDs + status transitions (no free text), so they need no scrub on
erasure — they remain as an anonymous audit trail. Registered as the `graph_retention` job so an
operator can enqueue it on a schedule.
"""
from __future__ import annotations

import time
from contextlib import closing

from .db import connect, migrate, platform_db_path
from .intros import IntroStore
from .notifications import NotificationStore

_REPUDIATION_PURGE_S = 30 * 86400  # hard-delete a decided/expired DSR record after 30 days


def run_retention(path: str | None = None) -> dict:
    p = path or platform_db_path()
    migrate(p)
    expired = IntroStore(p).sweep_expired()
    notifications = NotificationStore(p).purge()
    mentor_offers = vouch_invites = 0
    try:  # stores/phase5.py lands with slice S2; retention must not hard-depend on it
        from .phase5 import MentorStore, VouchInviteStore
    except ImportError:
        pass
    else:
        mentor_offers = MentorStore(p).sweep_expired()
        vouch_invites = VouchInviteStore(p).sweep_expired()
    now = time.time()
    with closing(connect(p)) as conn:
        edges = conn.execute(
            "DELETE FROM graph_edges WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,)).rowcount
        intros = conn.execute(
            "DELETE FROM intro_requests WHERE purge_after IS NOT NULL AND purge_after < ?",
            (now,)).rowcount
        # A1/privacy F6: an undecided repudiation past its TTL expires AND sheds every asserted
        # field — third-party PII (and challenge material) never outlives the review window.
        repud_scrubbed = conn.execute(
            "UPDATE repudiation_requests SET status='expired', first=NULL, last=NULL, "
            "company=NULL, email_hash=NULL, challenge_hash=NULL, purge_after=? "
            "WHERE status='pending' AND expires_at < ?",
            (now + _REPUDIATION_PURGE_S, now)).rowcount
        repud_purged = conn.execute(
            "DELETE FROM repudiation_requests WHERE purge_after IS NOT NULL AND purge_after < ?",
            (now,)).rowcount
        admin_sessions = conn.execute(
            "DELETE FROM admin_sessions WHERE expires_at < ?", (now,)).rowcount
        conn.commit()
    return {"intros_expired": expired, "edges_purged": edges, "intros_purged": intros,
            "notifications_purged": notifications, "mentor_offers_expired": mentor_offers,
            "vouch_invites_expired": vouch_invites, "repudiations_scrubbed": repud_scrubbed,
            "repudiations_purged": repud_purged, "admin_sessions_purged": admin_sessions}
