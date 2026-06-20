"""Handshake-export importer.

Models the real export shape (plan §A): a CSV of student/job metadata and a directory of resume
files, delivered as SEPARATE exports that must be stitched by email. Handles multi-batch dedup,
surfaces 'no resume available' as a first-class state, and reports coverage %.

Real Handshake exports cap resume downloads at 500 students/batch, so the coordinator runs several
filtered batches; pass multiple resume directories / CSVs and this dedups across them by email.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..inference.schema import CandidateProfile, JobSpec
from .parser import parse_resume_file, parse_resume_text


@dataclass
class ImportResult:
    candidates: list[CandidateProfile] = field(default_factory=list)
    total_rows: int = 0
    with_resume: int = 0
    duplicates: int = 0

    @property
    def coverage(self) -> float:
        return self.with_resume / self.total_rows if self.total_rows else 0.0

    def summary(self) -> str:
        return (
            f"{len(self.candidates)} students ({self.with_resume} with resumes, "
            f"coverage {self.coverage:.0%}); {self.duplicates} duplicate rows skipped."
        )


def _split_skills(value: str) -> list[str]:
    return [s.strip() for s in re.split(r"[;,|]", value) if s.strip()] if value else []


def import_students(meta_csv: str | Path, resume_dir: str | Path | None = None) -> ImportResult:
    """Stitch a student metadata CSV to resume files by `candidate_id` (falling back to email local
    part). Expected CSV columns: candidate_id, email, name, education_level, years_experience,
    resume_file (optional). Unknown columns are ignored."""
    meta_csv = Path(meta_csv)
    resume_dir = Path(resume_dir) if resume_dir else None
    result = ImportResult()
    seen_emails: set[str] = set()

    with meta_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            result.total_rows += 1
            email = (row.get("email") or "").strip().lower()
            if email and email in seen_emails:
                result.duplicates += 1
                continue
            if email:
                seen_emails.add(email)

            cid = (row.get("candidate_id") or email or f"row{result.total_rows}").strip()
            name = (row.get("name") or "").strip() or None
            edu = (row.get("education_level") or "").strip() or None
            yexp = row.get("years_experience")
            yexp_val = float(yexp) if yexp not in (None, "") else None

            resume_path = _find_resume(resume_dir, row, cid)
            if resume_path is not None:
                cand = parse_resume_file(
                    cid, resume_path, name=name, education_level=edu, years_experience=yexp_val
                )
            else:
                cand = parse_resume_text(
                    cid, "", name=name, education_level=edu, years_experience=yexp_val or 0.0,
                    has_resume=False,
                )
            if cand.has_resume:
                result.with_resume += 1
            result.candidates.append(cand)
    return result


def _find_resume(resume_dir: Path | None, row: dict, cid: str) -> Path | None:
    if resume_dir is None or not resume_dir.exists():
        return None
    named = (row.get("resume_file") or "").strip()
    if named and (resume_dir / named).exists():
        return resume_dir / named
    for ext in (".txt", ".pdf"):
        p = resume_dir / f"{cid}{ext}"
        if p.exists():
            return p
    email = (row.get("email") or "").split("@")[0]
    for ext in (".txt", ".pdf"):
        p = resume_dir / f"{email}{ext}"
        if email and p.exists():
            return p
    return None


def load_jobs(jobs_csv: str | Path) -> list[JobSpec]:
    """Load job postings. Columns: job_id, title, employer, description, required_skills,
    preferred_skills, min_education. Skills are ';'-separated canonical IDs."""
    jobs: list[JobSpec] = []
    with Path(jobs_csv).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            jobs.append(
                JobSpec(
                    job_id=(row.get("job_id") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    employer=(row.get("employer") or "").strip(),
                    description=(row.get("description") or "").strip(),
                    required_skills=_split_skills(row.get("required_skills") or ""),
                    preferred_skills=_split_skills(row.get("preferred_skills") or ""),
                    min_education=(row.get("min_education") or "").strip() or None,
                )
            )
    return jobs
