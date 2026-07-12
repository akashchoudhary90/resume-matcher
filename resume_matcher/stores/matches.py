"""Match results store (docs/IMPLEMENTATION.md Slice K). Boundary #4 is schema-enforced here:
the score_kind CHECK on match_results means this table can only ever hold honest fit/readiness
scores; the full ScoreResult (evidence quotes, reconciled breakdown) rides in result_json.
"""
from __future__ import annotations

import json
import time
from contextlib import closing

from ..api.serialize import result_to_dict
from ..inference.schema import ScoreResult
from .db import connect, migrate, platform_db_path

ENGINE_VERSION = "platform-v1"


class MatchStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or platform_db_path()
        migrate(self.path)

    def _conn(self):
        return connect(self.path)

    def upsert(self, posting_id: str, student_id: int, result: ScoreResult) -> None:
        payload = result_to_dict(result)
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO match_results(posting_id, student_id, fit_score, grade, result_json, "
                "engine_version, computed_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(posting_id, student_id) DO UPDATE SET fit_score=excluded.fit_score, "
                "grade=excluded.grade, result_json=excluded.result_json, "
                "engine_version=excluded.engine_version, computed_at=excluded.computed_at",
                (posting_id, student_id, result.fit_score, result.grade, json.dumps(payload),
                 ENGINE_VERSION, time.time()),
            )
            conn.commit()

    def delete_for_student(self, student_id: int) -> None:
        """Consent revoked / resume deleted: existing scores go too (the pool filter handles
        future runs; this removes the already-computed rows)."""
        with closing(self._conn()) as conn:
            conn.execute("DELETE FROM match_results WHERE student_id=?", (student_id,))
            conn.commit()

    def shortlist(self, posting_id: str) -> list[dict]:
        """Ranked candidates for a posting, joined with any application. Students appear by an
        opaque ref — never name/contact (those live behind the contact consent on applications)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT m.student_id, m.fit_score, m.grade, m.result_json, m.computed_at, "
                "a.id AS application_id, a.status AS application_status, "
                "a.human_review_requested "
                "FROM match_results m LEFT JOIN applications a "
                "ON a.posting_id = m.posting_id AND a.student_id = m.student_id "
                "WHERE m.posting_id=? ORDER BY m.fit_score DESC, m.student_id",
                (posting_id,),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["candidate_ref"] = f"student-{row.pop('student_id')}"
            row["result"] = json.loads(row.pop("result_json"))
            row["score_kind"] = "fit_readiness_not_hire_probability"
            out.append(row)
        return out

    def roles_for(self, student_id: int) -> list[dict]:
        """The student's 'roles for you' view over LIVE postings, best fit first."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT m.posting_id, p.title, o.name AS org_name, p.location, p.work_mode, "
                "p.employment_type, p.apply_deadline, m.fit_score, m.grade, m.result_json, "
                "a.id AS application_id, a.status AS application_status "
                "FROM match_results m JOIN postings p ON p.id = m.posting_id "
                "LEFT JOIN orgs o ON o.id = p.org_id "
                "LEFT JOIN applications a ON a.posting_id = m.posting_id AND a.student_id = m.student_id "
                "WHERE m.student_id=? AND p.status='live' "
                "ORDER BY m.fit_score DESC, m.posting_id",
                (student_id,),
            ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            result = json.loads(row.pop("result_json"))
            # the student-facing "why": explanation + gaps, not the raw engine payload
            row["explanation"] = result.get("explanation")
            row["gaps"] = result.get("gaps", [])
            row["score_kind"] = "fit_readiness_not_hire_probability"
            out.append(row)
        return out
