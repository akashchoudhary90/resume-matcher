"""End-to-end matching pipeline: retrieve -> rerank -> evaluate -> rank -> coach.

Produces, per job, a ranked shortlist of fit/readiness scores with coaching, and per candidate the
roles they are closest to. The set of shortlisted candidates per job is the 'selection' the bias
audit later analyzes (audit/metrics.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..inference.adapter import InferenceAdapter, get_adapter
from ..inference.schema import CandidateProfile, JobSpec, ScoreResult
from . import coaching
from .evaluator import evaluate
from .retrieval import retrieve
from .rerank import rerank


@dataclass
class JobShortlist:
    job: JobSpec
    ranked: list[ScoreResult] = field(default_factory=list)
    coaching: list[dict] = field(default_factory=list)

    @property
    def selected_ids(self) -> list[str]:
        return [r.candidate_id for r in self.ranked]


@dataclass
class MatchingRun:
    shortlists: list[JobShortlist] = field(default_factory=list)
    closest_fit: dict[str, coaching.ClosestFit] = field(default_factory=dict)

    def selections(self) -> dict[str, list[str]]:
        """job_id -> shortlisted candidate_ids (the 'selected' set for the bias audit)."""
        return {sl.job.job_id: sl.selected_ids for sl in self.shortlists}


def run_matching(
    candidates: list[CandidateProfile],
    jobs: list[JobSpec],
    adapter: InferenceAdapter | None = None,
    retrieve_k: int = 25,
    shortlist_k: int = 10,
) -> MatchingRun:
    adapter = adapter or get_adapter()
    by_id = {c.candidate_id: c for c in candidates}
    run = MatchingRun()
    per_candidate: dict[str, list[tuple[JobSpec, ScoreResult]]] = {}

    for job in jobs:
        retrieved = retrieve(job, candidates, top_k=retrieve_k)
        retrieved = rerank(job, by_id, retrieved, top_k=retrieve_k)
        results: list[ScoreResult] = []
        for r in retrieved:
            cand = by_id.get(r.candidate_id)
            if cand is None:
                continue
            res = evaluate(cand, job, adapter)
            results.append(res)
            per_candidate.setdefault(cand.candidate_id, []).append((job, res))
        results.sort(key=lambda x: x.fit_score, reverse=True)
        top = results[:shortlist_k]
        run.shortlists.append(
            JobShortlist(job=job, ranked=top, coaching=[coaching.coach(r, job) for r in top])
        )

    for cid, scored in per_candidate.items():
        run.closest_fit[cid] = coaching.closest_fit(cid, scored)
    return run
