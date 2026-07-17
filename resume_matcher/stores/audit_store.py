"""Persistent AUDIT PLANE — a physically separate SQLite file (docs/IMPLEMENTATION.md Slice W).

Boundary #2 made durable: voluntary self-ID lives in `data/audit.db` (RM_AUDIT_DB), NEVER in the
platform/scoring DB, and no connection in this codebase ever opens both files (CI greps the
platform schema for protected columns; this module never imports stores/db.py's connect).

The ONLY egress is `aggregate(refs, attr)` — counts over a caller-supplied list of opaque
candidate refs, with MIN-CELL suppression so small groups can't be re-identified. There is
deliberately no method that returns an individual's attributes, mirroring stores/data_planes.py.

Cell suppression alone is not privacy: an exact `responses` total lets a reader subtract the visible
cells and recover the hidden one, and re-running an aggregate as the ref-set grows by one differences
that student out. So `aggregate` also enforces a cohort floor (2*MIN_CELL) and bands `responses`
whenever anything was suppressed, and `report_snapshots` pins a served payload until the ref set has
moved by at least MIN_CELL (serving policy lives in the route).
"""
from __future__ import annotations

import json
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
            # Audit-plane table, created here and NEVER in a platform migration (boundary #2: the
            # two DBs share no migration runner). Pins a computed report so repeated reads of a
            # growing cohort can't be differenced down to one student.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS report_snapshots("
                "report_key TEXT NOT NULL, school_id INTEGER NOT NULL, payload_json TEXT NOT NULL, "
                "refs_count INTEGER NOT NULL, computed_at REAL NOT NULL, "
                "PRIMARY KEY(report_key, school_id))"
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
        response also reports how many members were hidden by suppression (never which).

        Two egress rules beyond per-cell suppression:
          * COHORT FLOOR — a cohort under 2*MIN_CELL responses publishes nothing at all (not even an
            exact total): with that few respondents even a visible cell names people.
          * COMPLEMENTARY SUPPRESSION — once any cell is hidden, the exact total is itself a
            disclosure (hidden cell = total - visible cells), so `responses` degrades to a
            MIN_CELL-wide band string ('35-40'). Callers must treat it as opaque, not arithmetic."""
        if attr not in AUDITABLE_ATTRIBUTES:
            raise ValueError(f"Not an auditable attribute: {attr!r}")
        if not candidate_refs:
            return {"counts": {}, "suppressed_cells": 0, "responses": 0, "min_cell": MIN_CELL}
        marks = ",".join("?" * len(candidate_refs))
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"SELECT value, COUNT(*) AS n FROM self_id WHERE attr=? "
                f"AND candidate_ref IN ({marks}) GROUP BY value",
                (attr, *candidate_refs),
            ).fetchall()
        counts = {r["value"]: r["n"] for r in rows}
        responses = sum(counts.values())
        if responses < 2 * MIN_CELL:
            return {"counts": {}, "suppressed_cells": len(counts), "responses": None,
                    "note": "cohort below reporting floor", "min_cell": MIN_CELL}
        visible = {v: n for v, n in counts.items() if n >= MIN_CELL}
        suppressed_cells = len(counts) - len(visible)
        out: dict = {
            "counts": visible,
            "suppressed_cells": suppressed_cells,
            "responses": responses,
            "min_cell": MIN_CELL,
        }
        if suppressed_cells:
            lo = (responses // MIN_CELL) * MIN_CELL
            out["responses"] = f"{lo}-{lo + MIN_CELL}"
            out["note"] = "total banded: subtracting visible cells would reveal a suppressed one"
        return out

    def save_snapshot(self, report_key: str, school_id: int, payload: dict, refs_count: int) -> None:
        """Pin a computed report. `refs_count` is the cohort size it was computed over — the route's
        staleness policy needs both it and computed_at to decide whether recomputing is safe."""
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO report_snapshots(report_key, school_id, payload_json, refs_count, "
                "computed_at) VALUES(?,?,?,?,?) ON CONFLICT(report_key, school_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, refs_count=excluded.refs_count, "
                "computed_at=excluded.computed_at",
                (report_key, school_id, json.dumps(payload, sort_keys=True), refs_count, time.time()),
            )
            conn.commit()

    def get_snapshot(self, report_key: str, school_id: int) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT payload_json, refs_count, computed_at FROM report_snapshots "
                "WHERE report_key=? AND school_id=?",
                (report_key, school_id),
            ).fetchone()
        if row is None:
            return None
        return {"payload": json.loads(row["payload_json"]), "refs_count": row["refs_count"],
                "computed_at": row["computed_at"]}
