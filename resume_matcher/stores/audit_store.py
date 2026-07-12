"""Persistent AUDIT PLANE — a physically separate SQLite file (docs/IMPLEMENTATION.md Slice W).

Boundary #2 made durable: voluntary self-ID lives in `data/audit.db` (RM_AUDIT_DB), NEVER in the
platform/scoring DB, and no connection in this codebase ever opens both files (CI greps the
platform schema for protected columns; this module never imports stores/db.py's connect).

The ONLY egress is `aggregate(refs, attr)` — counts over a caller-supplied list of opaque
candidate refs, with MIN-CELL suppression so small groups can't be re-identified. There is
deliberately no method that returns an individual's attributes, mirroring stores/data_planes.py.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing

from .data_planes import AUDITABLE_ATTRIBUTES

MIN_CELL = 5  # cells with fewer members are suppressed in every aggregate


def audit_db_path() -> str:
    return os.environ.get("RM_AUDIT_DB") or os.path.join("data", "audit.db")


class AuditDB:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or audit_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with closing(self._conn()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS self_id("
                "candidate_ref TEXT NOT NULL, attr TEXT NOT NULL, value TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY(candidate_ref, attr))"
            )
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def set_self_id(self, candidate_ref: str, attrs: dict[str, str]) -> int:
        """Store/replace voluntary self-ID answers. Unknown attributes are rejected — the audit
        plane only ever holds the enumerated AUDITABLE_ATTRIBUTES."""
        clean = {k: str(v).strip()[:60] for k, v in (attrs or {}).items()
                 if k in AUDITABLE_ATTRIBUTES and str(v or "").strip()}
        bad = set(attrs or {}) - AUDITABLE_ATTRIBUTES
        if bad:
            raise ValueError(f"Not auditable attribute(s): {sorted(bad)}")
        now = time.time()
        with closing(self._conn()) as conn:
            for attr, value in clean.items():
                conn.execute(
                    "INSERT INTO self_id(candidate_ref, attr, value, at) VALUES(?,?,?,?) "
                    "ON CONFLICT(candidate_ref, attr) DO UPDATE SET value=excluded.value, "
                    "at=excluded.at",
                    (candidate_ref, attr, value, now),
                )
            conn.commit()
        return len(clean)

    def delete_self_id(self, candidate_ref: str) -> bool:
        with closing(self._conn()) as conn:
            cur = conn.execute("DELETE FROM self_id WHERE candidate_ref=?", (candidate_ref,))
            conn.commit()
            return cur.rowcount > 0

    def has_self_id(self, candidate_ref: str) -> bool:
        with closing(self._conn()) as conn:
            return conn.execute("SELECT 1 FROM self_id WHERE candidate_ref=? LIMIT 1",
                                (candidate_ref,)).fetchone() is not None

    def aggregate(self, candidate_refs: list[str], attr: str) -> dict:
        """{value: count} over the given refs for one attribute, MIN-CELL-suppressed. The
        response also reports how many members were hidden by suppression (never which)."""
        if attr not in AUDITABLE_ATTRIBUTES:
            raise ValueError(f"Not an auditable attribute: {attr!r}")
        if not candidate_refs:
            return {"counts": {}, "suppressed_cells": 0, "responses": 0}
        marks = ",".join("?" * len(candidate_refs))
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"SELECT value, COUNT(*) AS n FROM self_id WHERE attr=? "
                f"AND candidate_ref IN ({marks}) GROUP BY value",
                (attr, *candidate_refs),
            ).fetchall()
        counts = {r["value"]: r["n"] for r in rows}
        visible = {v: n for v, n in counts.items() if n >= MIN_CELL}
        return {
            "counts": visible,
            "suppressed_cells": len(counts) - len(visible),
            "responses": sum(counts.values()),
            "min_cell": MIN_CELL,
        }
