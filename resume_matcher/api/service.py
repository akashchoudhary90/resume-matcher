"""Application state + view-builders behind the web API.

Holds the currently loaded dataset, the matching run, and the audit store in memory (fine for a
single-coordinator prototype). The web endpoints are thin wrappers over this. Real ingestion (after
privacy sign-off) plugs in by replacing `load_synthetic` with `load_from_export`.
"""
from __future__ import annotations

import csv
import os
from collections import Counter
from pathlib import Path

import numpy as np

from ..audit.metrics import AuditReport, exposure_parity, homophily_disparity, selection_audit
from ..audit.proxy_leakage import proxy_leakage
from ..ingestion.importer import import_students, load_jobs
from ..ingestion.synthetic import generate_dataset
from ..inference.adapter import get_adapter
from ..inference.schema import CandidateProfile
from ..matching.pipeline import MatchingRun, run_matching
from ..stores.data_planes import AuditStore
from .serialize import result_to_dict

DATA = Path("data/synthetic")


class AppState:
    def __init__(self) -> None:
        self.candidates: list[CandidateProfile] = []
        self.jobs = []
        self.run: MatchingRun | None = None
        self.audit_store = AuditStore()
        self.summary: str = "No data loaded."
        self.backend: str = os.environ.get("RM_INFERENCE_BACKEND", "mock")

    # ---- loading -----------------------------------------------------------------
    def load_synthetic(self, n_students: int = 60, n_jobs: int = 12, seed: int = 42) -> dict:
        if not (DATA / "students.csv").exists():
            generate_dataset(DATA, n_students=n_students, n_jobs=n_jobs, seed=seed)
        return self._load_from(DATA)

    def regenerate_synthetic(self, n_students: int = 60, n_jobs: int = 12, seed: int = 42) -> dict:
        generate_dataset(DATA, n_students=n_students, n_jobs=n_jobs, seed=seed)
        return self._load_from(DATA)

    def _load_from(self, data_dir: Path) -> dict:
        imported = import_students(data_dir / "students.csv", data_dir / "resumes")
        self.candidates = imported.candidates
        self.jobs = load_jobs(data_dir / "jobs.csv")
        adapter = get_adapter(self.backend)
        self.run = run_matching(self.candidates, self.jobs, adapter=adapter, retrieve_k=30, shortlist_k=10)
        self._load_self_id(data_dir / "self_id.csv")
        self.summary = imported.summary()
        return self.status()

    def _load_self_id(self, path: Path) -> None:
        self.audit_store = AuditStore()
        if not path.exists():
            return
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                attrs = {k: row[k] for k in ("race_ethnicity", "gender") if row.get(k)}
                if attrs:
                    self.audit_store.record_self_id(row["candidate_id"], attrs)

    # ---- views -------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "loaded": self.run is not None,
            "backend": self.backend,
            "summary": self.summary,
            "n_candidates": len(self.candidates),
            "n_jobs": len(self.jobs),
            "score_kind": "fit_readiness_not_hire_probability",
        }

    def jobs_overview(self) -> list[dict]:
        if not self.run:
            return []
        out = []
        for sl in self.run.shortlists:
            top = sl.ranked[0].fit_score if sl.ranked else None
            out.append(
                {
                    "job_id": sl.job.job_id,
                    "title": sl.job.title,
                    "employer": sl.job.employer,
                    "required_skills": sl.job.required_skills,
                    "shortlisted": len(sl.ranked),
                    "top_fit": top,
                }
            )
        return out

    def job_shortlist(self, job_id: str) -> dict | None:
        if not self.run:
            return None
        sl = next((s for s in self.run.shortlists if s.job.job_id == job_id), None)
        if sl is None:
            return None
        rows = [result_to_dict(r, coach) for r, coach in zip(sl.ranked, sl.coaching)]
        return {
            "job": {"job_id": sl.job.job_id, "title": sl.job.title, "employer": sl.job.employer,
                     "required_skills": sl.job.required_skills, "preferred_skills": sl.job.preferred_skills},
            "shortlist": rows,
        }

    def candidate_view(self, cid: str) -> dict | None:
        cand = next((c for c in self.candidates if c.candidate_id == cid), None)
        if cand is None:
            return None
        cf = self.run.closest_fit.get(cid) if self.run else None
        return {
            "candidate_id": cand.candidate_id,
            "skills": cand.skills,
            "education_level": cand.education_level,
            "years_experience": cand.years_experience,
            "has_resume": cand.has_resume,
            "closest_fit": cf.ranked if cf else [],
        }

    def candidate_ids(self) -> list[str]:
        return [c.candidate_id for c in self.candidates]

    def audit(self) -> dict:
        if not self.run or not self.audit_store.has_data():
            return {"available": False, "reason": "load data with self-ID first"}
        pool = self.candidate_ids()
        selected = set().union(*[set(s.selected_ids) for s in self.run.shortlists]) if self.run.shortlists else set()
        mask = [cid in selected for cid in pool]

        result: dict = {"available": True, "n_selected": sum(mask), "n_pool": len(pool), "attributes": {}}
        for attr in ("race_ethnicity", "gender"):
            labels = self.audit_store.labels_for(pool, attr)
            report = selection_audit(labels, mask, attribute=attr, min_cell=5)
            result["attributes"][attr] = _report_to_dict(report)

        # Rank-aware exposure parity: a group can pass top-k parity yet be ranked systematically
        # lower. Use each candidate's BEST (smallest) position across the per-job shortlists.
        best_rank: dict[str, int] = {}
        for sl in self.run.shortlists:
            for pos, r in enumerate(sl.ranked, start=1):
                if r.candidate_id not in best_rank or pos < best_rank[r.candidate_id]:
                    best_rank[r.candidate_id] = pos
        ranks = [best_rank.get(cid, len(pool) + 1) for cid in pool]
        race_labels = self.audit_store.labels_for(pool, "race_ethnicity")
        result["exposure"] = exposure_parity(race_labels, ranks, min_cell=5)

        # Homophily disparity (the reframed hunch): reference = modal self-identified race group.
        present = [lab for lab in race_labels if lab]
        if present:
            ref = Counter(present).most_common(1)[0][0]
            homophily = homophily_disparity(race_labels, mask, reference_group=ref, min_cell=5)
            # Be explicit that the reference is the modal CANDIDATE group, NOT the hiring team's
            # dominant group (which the audit ideally compares against but we don't collect here) —
            # so the metric isn't misread as a true in-group/out-group hiring-team comparison.
            homophily["reference_basis"] = (
                "modal self-identified candidate group (hiring-team composition not collected)"
            )
            result["homophily"] = homophily

        # Proxy-leakage diagnostic on the scoring features.
        feats = np.array(
            [[len(c.skills), c.years_experience, len(c.text)] for c in self.candidates], dtype=float
        )
        target = Counter(present).most_common(1)[0][0] if present else None
        if target:
            result["proxy_leakage"] = proxy_leakage(feats, race_labels, target_group=target)
        return result


def _report_to_dict(r: AuditReport) -> dict:
    return {
        "attribute": r.attribute,
        "four_fifths_pass": r.four_fifths_pass,
        "min_impact_ratio": r.min_impact_ratio,
        "flagged": r.flagged(),
        "notes": r.notes,
        "groups": [
            {
                "group": g.group,
                "n": g.n,
                "selected": g.selected,
                "selection_rate": round(g.selection_rate, 3),
                "impact_ratio": g.impact_ratio,
                "fisher_p": g.fisher_p,
                "below_min_cell": g.below_min_cell,
            }
            for g in r.groups
        ],
    }


_STATE = AppState()


def get_state() -> AppState:
    return _STATE
