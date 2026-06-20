"""Stage 2 rerank (optional precision boost).

If sentence-transformers' CrossEncoder is available, rerank the retrieved shortlist with it;
otherwise keep the retrieval order. Reranking only narrows/re-orders the shortlist that the LLM
evaluator then scores — it never sets the final fit score (that's ranker.py).
"""
from __future__ import annotations

from ..inference.schema import CandidateProfile, JobSpec
from .retrieval import Retrieved
from .taxonomy import canonical_name

_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def rerank(
    job: JobSpec,
    candidates_by_id: dict[str, CandidateProfile],
    retrieved: list[Retrieved],
    top_k: int | None = None,
) -> list[Retrieved]:
    top_k = top_k or len(retrieved)
    try:  # pragma: no cover - optional dep
        from sentence_transformers import CrossEncoder
    except Exception:
        return retrieved[:top_k]

    model = CrossEncoder(_CROSS_ENCODER_MODEL)
    job_text = f"{job.title} {' '.join(canonical_name(s) for s in job.required_skills)} {job.description}"
    pairs, ids = [], []
    for r in retrieved:
        c = candidates_by_id.get(r.candidate_id)
        if not c:
            continue
        pairs.append((job_text, f"{' '.join(canonical_name(s) for s in c.skills)} {c.text}"))
        ids.append(r.candidate_id)
    if not pairs:
        return retrieved[:top_k]
    scores = model.predict(pairs)
    ranked = sorted(zip(ids, scores), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [Retrieved(candidate_id=cid, score=float(s)) for cid, s in ranked]
