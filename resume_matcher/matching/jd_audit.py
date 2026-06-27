"""JD reverse-audit — does the JOB POSTING's own requirements over-filter the pool?

Lost candidates (and a lot of bias) enter at the requirements, not the model. On a glass-box,
deterministic ranker we can answer something a black-box screener cannot: for each requirement (a
must-have, a required skill, the minimum education, the minimum years), how many otherwise-qualified
candidates does it screen out — and which candidates are blocked SOLELY by it, i.e. relax that one thing
and they would clear the bar? We re-score on the same ranker, so every count is exact.

This is an unoccupied category: nobody audits the posting before it screens anyone. Honesty: it reports
POOL IMPACT (who clears the bar), NOT who to hire and NOT protected-group disparity (the demo holds no
protected data — that audit lives in audit/metrics.py over the synthetic dashboard).
"""
from __future__ import annotations

from ..inference.schema import CandidateProfile, JobSpec, MatchExtraction, ScoreResult
from . import ranker as _ranker
from .taxonomy import canonical_name

_QUALIFIED = 65.0          # grade B+ — "would clear the shortlist bar"
_INTEGRITY_PREFIXES = ("stuffing:", "injection:", "hidden_text:")


def _requirements(job: JobSpec) -> list[dict]:
    reqs: list[dict] = []
    must = set(job.must_have_skills)
    for sid in dict.fromkeys(job.must_have_skills):
        reqs.append({"kind": "must_have", "key": sid, "label": f"must-have: {canonical_name(sid)}"})
    for sid in dict.fromkeys(job.required_skills):
        if sid not in must:
            reqs.append({"kind": "required", "key": sid, "label": f"required skill: {canonical_name(sid)}"})
    if job.min_education:
        reqs.append({"kind": "education", "key": None, "label": f"minimum education: {job.min_education}"})
    if job.min_years and job.min_years > 0:
        reqs.append({"kind": "experience", "key": None, "label": f"minimum experience: {job.min_years:g} yrs"})
    return reqs


def _relax(job: JobSpec, req: dict) -> JobSpec:
    """Return a copy of the job with one requirement dropped."""
    update: dict = {}
    if req["kind"] == "must_have":
        update["must_have_skills"] = [s for s in job.must_have_skills if s != req["key"]]
        update["required_skills"] = [s for s in job.required_skills if s != req["key"]]
    elif req["kind"] == "required":
        update["required_skills"] = [s for s in job.required_skills if s != req["key"]]
        update["must_have_skills"] = [s for s in job.must_have_skills if s != req["key"]]
    elif req["kind"] == "education":
        update["min_education"] = None
    elif req["kind"] == "experience":
        update["min_years"] = None
    return job.model_copy(update=update)


def _rescore(result: ScoreResult, candidate: CandidateProfile, job: JobSpec) -> float:
    """Re-score the candidate's verified evidence against a (relaxed) job — exact, same ranker."""
    integ = [f for f in result.flags if any(f.startswith(p) for p in _INTEGRITY_PREFIXES)]
    extraction = MatchExtraction(candidate_id=candidate.candidate_id, job_id=job.job_id,
                                 skill_matches=list(result.verified_matches))
    return _ranker.score(extraction, candidate, job, extra_flags=integ).fit_score


def audit_requirements(scored: list[tuple], job: JobSpec, *, qualified: float = _QUALIFIED) -> dict | None:
    """Audit how the job's requirements shape the qualified pool.

    `scored` is a list of (ScoreResult, CandidateProfile, label). Returns None when there's nothing to
    say (too few candidates, or no relaxable requirement)."""
    cands = [(res, cand, label) for (res, cand, label) in scored if cand is not None]
    if len(cands) < 2:
        return None
    reqs = _requirements(job)
    if not reqs:
        return None

    n = len(cands)
    currently_qualified = sum(1 for res, _, _ in cands if res.fit_score >= qualified)
    below = [(res, cand, label) for (res, cand, label) in cands if res.fit_score < qualified]

    findings: list[dict] = []
    # freed_by[label] = set of requirement labels that, relaxed ALONE, lift this candidate over the bar
    freed_by: dict[str, set[str]] = {label: set() for _, _, label in below}
    for req in reqs:
        relaxed = _relax(job, req)
        freed: list[str] = []
        for res, cand, label in below:
            if _rescore(res, cand, relaxed) >= qualified:
                freed.append(label)
                freed_by[label].add(req["label"])
        findings.append({"requirement": req["label"], "kind": req["kind"],
                         "freed_count": len(freed), "freed": freed[:6]})

    # A "sole blocker": a candidate lifted over the bar by relaxing EXACTLY one requirement.
    for f in findings:
        f["sole_blocked"] = sum(1 for label in f["freed"] if len(freed_by.get(label, ())) == 1)
    findings = [f for f in findings if f["freed_count"] > 0]
    findings.sort(key=lambda f: (-f["sole_blocked"], -f["freed_count"]))

    top = findings[0] if findings else None
    if top is None:
        summary = (f"{currently_qualified} of {n} candidates clear the bar (grade B+). No single "
                   f"requirement is the sole blocker for anyone below it.")
    else:
        summary = (f"{currently_qualified} of {n} candidates clear the bar (grade B+). The most-limiting "
                   f"requirement is '{top['requirement']}': relaxing it alone would add {top['freed_count']} "
                   f"more candidate(s)" + (f" ({top['sole_blocked']} blocked solely by it)"
                                           if top["sole_blocked"] else "")
                   + " — worth checking it's truly required.")
    return {
        "n_candidates": n,
        "qualified": currently_qualified,
        "bar": "grade B+ (fit >= 65)",
        "findings": findings,
        "summary": summary,
    }
