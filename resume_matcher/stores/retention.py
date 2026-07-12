"""Relationship-graph retention/erasure job (docs/RELATIONSHIPS.md Slice AK).

Enforces the retention schedule so PII does not persist indefinitely (a FIPPA/PIPEDA obligation
the adversarial review flagged):
  * self-upload / derived graph_edges are hard-deleted once past their expires_at (default 12mo);
  * intro_requests are hard-deleted once past purge_after (set on a terminal status, +6mo);
  * requested intros past their expiry are swept to 'expired' first (so they then get a purge_after).

intro_events carry only opaque IDs + status transitions (no free text), so they need no scrub on
erasure — they remain as an anonymous audit trail. Registered as the `graph_retention` job so an
operator can enqueue it on a schedule.
"""
from __future__ import annotations

import time
from contextlib import closing

from .db import connect, migrate, platform_db_path
from .intros import IntroStore


def run_retention(path: str | None = None) -> dict:
    p = path or platform_db_path()
    migrate(p)
    expired = IntroStore(p).sweep_expired()
    now = time.time()
    with closing(connect(p)) as conn:
        edges = conn.execute(
            "DELETE FROM graph_edges WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,)).rowcount
        intros = conn.execute(
            "DELETE FROM intro_requests WHERE purge_after IS NOT NULL AND purge_after < ?",
            (now,)).rowcount
        conn.commit()
    return {"intros_expired": expired, "edges_purged": edges, "intros_purged": intros}
