"""Stage 1 retrieval: embed candidates + job into one space and rank candidates by cosine.

Cheap recall over the whole pool; the expensive LLM evaluator only runs on the top-k survivors.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..inference.schema import CandidateProfile, JobSpec
from .embeddings import Embedder, cosine_scores
from .taxonomy import canonical_name


@dataclass
class Retrieved:
    candidate_id: str
    score: float


def _candidate_doc(c: CandidateProfile) -> str:
    skills = " ".join(canonical_name(s) for s in c.skills)
    return f"{skills}\n{c.text}"


def _job_doc(j: JobSpec) -> str:
    skills = " ".join(canonical_name(s) for s in (j.required_skills + j.preferred_skills))
    return f"{j.title} {j.employer} {skills}\n{j.description}"


def retrieve(
    job: JobSpec,
    candidates: list[CandidateProfile],
    top_k: int = 25,
    embedder: Embedder | None = None,
) -> list[Retrieved]:
    """Return the top_k candidates for `job`, ranked by embedding cosine similarity."""
    if not candidates:
        return []
    embedder = embedder or Embedder()
    cand_docs = [_candidate_doc(c) for c in candidates]
    job_doc = _job_doc(job)
    embedder.fit(cand_docs + [job_doc])  # no-op for semantic backend; builds vocab for TF-IDF
    cand_matrix = embedder.encode(cand_docs)
    job_vec = embedder.encode([job_doc])[0]
    scores = cosine_scores(job_vec, cand_matrix)
    order = scores.argsort()[::-1][:top_k]
    return [Retrieved(candidate_id=candidates[i].candidate_id, score=float(scores[i])) for i in order]
