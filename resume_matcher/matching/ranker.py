"""Deterministic ranker — where the scoring DECISION is made (never by the LLM).

It takes the LLM's MatchExtraction and:
  1. VERIFIES every claimed evidence span is a real verbatim substring of the candidate's text.
     Unverifiable matches are discarded and flagged — this defeats fabricated/injected skill claims.
  2. Computes an explainable fit/readiness score that "thinks like a recruiter":
       - skills are weighted by importance (must-have 2x > required > preferred),
       - a missing MUST-HAVE (deal-breaker) heavily penalizes the score,
       - below the job's minimum experience applies a graded penalty,
       - below the minimum education applies a fixed penalty.
     The number is reproducible and decomposed into a point-by-point `ScoreExplanation` whose line
     items + multipliers reconcile EXACTLY to `fit_score`.

Backward-compatible: with no must-haves and no min_years, scoring matches the prior 75/25 model.

This is the honesty boundary: `fit_score` is fit/readiness, NOT a predicted probability of hire.
"""
from __future__ import annotations

import re

from ..inference.schema import (
    CandidateProfile,
    Confidence,
    Importance,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    ScoreComponent,
    ScoreExplanation,
    ScoreResult,
    SkillEvidence,
)
from .taxonomy import canonical_name

_EDU_RANK = {
    "highschool": 0, "high school": 0, "diploma": 1, "certificate": 1, "associate": 2,
    "bachelor": 3, "bachelors": 3, "master": 4, "masters": 4, "phd": 5, "doctorate": 5,
}

_STATUS_WEIGHT = {MatchStatus.match: 1.0, MatchStatus.partial: 0.5, MatchStatus.missing: 0.0}

_REQUIRED_WEIGHT = 75.0
_PREFERRED_WEIGHT = 25.0
_BELOW_EDU_FACTOR = 0.85
_MUST_HAVE_WEIGHT = 2.0          # a must-have skill weighs 2x a regular required skill
_MISSING_MUST_HAVE_FACTOR = 0.4  # missing a deal-breaker heavily penalizes (but never auto-rejects)
_MIN_EXPERIENCE_FACTOR = 0.7     # floor of the graded experience penalty
_MAX_EVIDENCE_SPAN = 160
_MIN_EVIDENCE_ALNUM = 3          # a quote with fewer alphanumerics is too generic to be evidence

_WS_RE = re.compile(r"\s+")


def _norm_ws(s: str) -> str:
    """Collapse runs of whitespace to a single space (and trim) so verification tolerates layout
    differences between the resume text and a model's transcription, without becoming more lenient
    about actual content."""
    return _WS_RE.sub(" ", s).strip()


def _edu_rank(level: str | None) -> int | None:
    return _EDU_RANK.get(level.strip().lower()) if level else None


def _verify(norm_text: str, ev: SkillEvidence) -> bool:
    """True only when the claimed quote is a MEANINGFUL verbatim substring of the candidate's text.

    `norm_text` is the candidate text, already whitespace-normalized + lowercased by the caller.
    Degenerate spans — a lone space, a single character, or a 1-2 char token — are rejected even if
    technically present, because they are a substring of virtually any resume and would let an
    untrusted model (or an injected resume that steers it) fabricate a "verified" skill and move the
    score. This is the trust boundary that makes the verbatim-evidence guarantee real."""
    if ev.status == MatchStatus.missing or not ev.evidence_span:
        return False
    span = ev.evidence_span.strip()
    if sum(ch.isalnum() for ch in span) < _MIN_EVIDENCE_ALNUM:
        return False
    return _norm_ws(span).lower() in norm_text


def _clip_span(span: str | None) -> str | None:
    if not span:
        return None
    span = span.strip()
    return span if len(span) <= _MAX_EVIDENCE_SPAN else span[:_MAX_EVIDENCE_SPAN].rstrip() + "…"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _weighted_components(
    skill_ids: list[str],
    bucket_name: str,
    bucket_total: float,
    weight_of: dict[str, float],
    importance_of: dict[str, Importance],
    verified_by_skill: dict[str, SkillEvidence],
    discarded_by_skill: dict[str, SkillEvidence],
) -> tuple[list[ScoreComponent], float]:
    """One ScoreComponent per job skill, each weighted by importance. Displayed per-row points are
    cumulative-rounded so they sum EXACTLY to the rounded bucket total. Returns (components, earned)."""
    if not skill_ids or bucket_total <= 0:
        return [], 0.0
    total_w = sum(weight_of[s] for s in skill_ids) or 1.0
    comps: list[ScoreComponent] = []
    earned_total = 0.0
    cum_poss_raw = cum_poss_alloc = 0.0
    cum_earn_raw = cum_earn_alloc = 0.0

    for sid in skill_ids:
        skill_max = bucket_total * weight_of[sid] / total_w
        ev = verified_by_skill.get(sid)
        status_w = _STATUS_WEIGHT[ev.status] if ev is not None else 0.0
        earned_total += skill_max * status_w

        cum_poss_raw += skill_max
        row_possible = round(round(cum_poss_raw, 2) - cum_poss_alloc, 2)
        cum_poss_alloc = round(cum_poss_alloc + row_possible, 2)
        cum_earn_raw += skill_max * status_w
        row_earned = round(round(cum_earn_raw, 2) - cum_earn_alloc, 2)
        cum_earn_alloc = round(cum_earn_alloc + row_earned, 2)

        importance = importance_of[sid]
        if ev is not None:
            note = (
                "Found in the resume — quote verified verbatim."
                if ev.status == MatchStatus.match
                else "Partially evidenced — counted at half weight."
            )
            comps.append(ScoreComponent(
                skill_id=sid, skill_name=canonical_name(sid), bucket=bucket_name,
                importance=importance, status=ev.status, verified=True,
                evidence_span=_clip_span(ev.evidence_span),
                points_possible=row_possible, points_earned=row_earned, note=note,
            ))
            continue
        dev = discarded_by_skill.get(sid)
        if dev is not None:
            comps.append(ScoreComponent(
                skill_id=sid, skill_name=canonical_name(sid), bucket=bucket_name,
                importance=importance, status=dev.status, verified=False, evidence_span=None,
                points_possible=row_possible, points_earned=0.0,
                note="Claimed, but the supporting quote could not be found verbatim in the resume — "
                     "NOT counted (anti-fabrication / anti-injection safeguard).",
            ))
            continue
        comps.append(ScoreComponent(
            skill_id=sid, skill_name=canonical_name(sid), bucket=bucket_name,
            importance=importance, status=MatchStatus.missing, verified=False,
            points_possible=row_possible, points_earned=0.0, note="Not found in the resume.",
        ))
    return comps, earned_total


def _summary(fit, grade, comps, n_discarded, missing_must, edu_factor, exp_factor) -> str:
    real = [c for c in comps if c.bucket in ("required", "preferred")]
    got = sum(1 for c in real if c.verified and c.status == MatchStatus.match)
    parts = [f"Fit {fit:.1f} (grade {grade})."]
    if real:
        parts.append(f"Matched {got} of {len(real)} listed skills with verbatim evidence.")
    if missing_must:
        parts.append(f"Missing must-have skill(s): {', '.join(canonical_name(s) for s in missing_must)} "
                     f"— heavily penalized.")
    if n_discarded:
        parts.append(f"{n_discarded} claimed skill(s) could not be verified and were not counted.")
    if exp_factor < 1.0:
        parts.append("Below the job's minimum experience (penalty applied).")
    if edu_factor < 1.0:
        parts.append("Below the job's minimum education (penalty applied).")
    return " ".join(parts)


def score(
    extraction: MatchExtraction,
    candidate: CandidateProfile,
    job: JobSpec,
    extra_flags: list[str] | None = None,
) -> ScoreResult:
    flags = list(extra_flags or [])

    must_set = set(_dedupe(job.must_have_skills))
    # must-haves are required skills (weighted higher); merge so a must-have is always scored.
    req_ids = _dedupe(list(job.required_skills) + list(job.must_have_skills))
    req_set = set(req_ids)
    pref_ids = [s for s in _dedupe(job.preferred_skills) if s not in req_set]
    if (len(_dedupe(job.required_skills)) != len(job.required_skills)
            or len(pref_ids) != len(job.preferred_skills)):
        flags.append("duplicate_skill_ids_collapsed")
    job_skill_ids = req_set | set(pref_ids)

    verified: list[SkillEvidence] = []
    discarded: list[SkillEvidence] = []
    cand_text_norm = _norm_ws(candidate.text).lower()
    for ev in extraction.skill_matches:
        if ev.status == MatchStatus.missing or ev.skill_id not in job_skill_ids:
            continue  # off-spec or non-match -> never counted
        if _verify(cand_text_norm, ev):
            verified.append(ev)
        else:
            discarded.append(ev)
            flags.append(f"unverifiable_evidence:{ev.skill_id}")

    verified_by_skill: dict[str, SkillEvidence] = {}
    for ev in verified:
        prev = verified_by_skill.get(ev.skill_id)
        if prev is None or _STATUS_WEIGHT[ev.status] > _STATUS_WEIGHT[prev.status]:
            verified_by_skill[ev.skill_id] = ev
        if prev is not None:
            flags.append(f"duplicate_skill_evidence:{ev.skill_id}")
    discarded_by_skill = {ev.skill_id: ev for ev in discarded}

    if not req_ids:
        flags.append("no_required_skills")

    # Bucket weights: both present -> 75/25; only one -> that bucket gets 100.
    has_req, has_pref = bool(req_ids), bool(pref_ids)
    if has_req and has_pref:
        req_w, pref_w = _REQUIRED_WEIGHT, _PREFERRED_WEIGHT
    elif has_req:
        req_w, pref_w = 100.0, 0.0
    elif has_pref:
        req_w, pref_w = 0.0, 100.0
    else:
        req_w, pref_w = 0.0, 0.0

    # Per-skill importance + weight. Must-haves weigh 2x within the required bucket.
    importance_of: dict[str, Importance] = {}
    weight_of: dict[str, float] = {}
    for s in req_ids:
        is_must = s in must_set
        importance_of[s] = Importance.essential if is_must else Importance.important
        weight_of[s] = _MUST_HAVE_WEIGHT if is_must else 1.0
    for s in pref_ids:
        importance_of[s] = Importance.optional
        weight_of[s] = 1.0

    req_comps, req_earned = _weighted_components(
        req_ids, "required", req_w, weight_of, importance_of, verified_by_skill, discarded_by_skill)
    pref_comps, pref_earned = _weighted_components(
        pref_ids, "preferred", pref_w, weight_of, importance_of, verified_by_skill, discarded_by_skill)

    required_earned = round(req_earned, 2)
    preferred_earned = round(pref_earned, 2)
    subtotal = round(required_earned + preferred_earned, 2)

    # ---- multipliers ------------------------------------------------------------------------
    # Education gate.
    edu_factor, edu_note = 1.0, "The job specified no minimum education (no adjustment)."
    need_e, have_e = _edu_rank(job.min_education), _edu_rank(candidate.education_level)
    if need_e is not None and have_e is not None and have_e < need_e:
        edu_factor = _BELOW_EDU_FACTOR
        edu_note = (f"Listed education ({candidate.education_level}) is below the job minimum "
                    f"({job.min_education}); x{_BELOW_EDU_FACTOR} (self-reported).")
        flags.append("below_min_education")
    elif need_e is not None and have_e is not None:
        edu_note = "Education meets or exceeds the minimum (self-reported; no adjustment)."
    elif need_e is not None:
        edu_note = "Education level could not be determined from the resume (no adjustment)."

    # Experience gate (graded): below min_years scales between _MIN_EXPERIENCE_FACTOR and 1.0.
    exp_factor, exp_note = 1.0, "The job specified no minimum experience (no adjustment)."
    if job.min_years and job.min_years > 0:
        have_y = candidate.years_experience or 0.0
        if have_y < job.min_years:
            ratio = max(0.0, min(1.0, have_y / job.min_years))
            exp_factor = round(_MIN_EXPERIENCE_FACTOR + (1.0 - _MIN_EXPERIENCE_FACTOR) * ratio, 3)
            exp_note = (f"{have_y:g} yrs experience vs {job.min_years:g} required; "
                        f"x{exp_factor} (self-reported).")
            flags.append("below_min_experience")
        else:
            exp_note = f"Meets the minimum experience ({job.min_years:g}+ yrs; no adjustment)."

    # Must-have gate: a missing deal-breaker heavily penalizes (still listed, never auto-rejected).
    missing_must = [s for s in req_ids if s in must_set and s not in verified_by_skill]
    must_factor, must_note = 1.0, ""
    if must_set:
        if missing_must:
            must_factor = _MISSING_MUST_HAVE_FACTOR
            must_note = ("Missing must-have skill(s): "
                         f"{', '.join(canonical_name(s) for s in missing_must)} — x{must_factor}.")
            for s in missing_must:
                flags.append(f"missing_must_have:{s}")
        else:
            must_note = "All must-have skills are present (no adjustment)."

    fit = round(subtotal * edu_factor * exp_factor * must_factor, 1)
    grade = "A" if fit >= 80 else "B" if fit >= 65 else "C" if fit >= 50 else "D"

    info_comps: list[ScoreComponent] = []
    if not has_req and not has_pref:
        info_comps.append(ScoreComponent(
            skill_id="-", skill_name="(no skills provided)", bucket="info",
            status=MatchStatus.missing, verified=False, points_possible=0.0, points_earned=0.0,
            note="No required or preferred skills were provided, so no fit score can be computed."))

    explanation = ScoreExplanation(
        formula="fit = round( skills_subtotal x education x experience x must_have , 1 )",
        components=req_comps + pref_comps + info_comps,
        required_possible=round(req_w, 1),
        preferred_possible=round(pref_w, 1),
        required_earned=required_earned,
        preferred_earned=preferred_earned,
        subtotal=subtotal,
        education_factor=edu_factor,
        education_note=edu_note,
        experience_factor=exp_factor,
        experience_note=exp_note,
        must_have_factor=must_factor,
        must_have_note=must_note,
        final_score=fit,
        summary=_summary(fit, grade, req_comps + pref_comps, len(discarded), missing_must,
                         edu_factor, exp_factor),
    )

    if not candidate.has_resume or len(candidate.text) < 200:
        conf = Confidence.low
    elif discarded:
        conf = Confidence.medium
    else:
        conf = Confidence.high

    subscores = {
        "required_coverage": round(req_earned / req_w, 3) if req_w else 0.0,
        "preferred_coverage": round(pref_earned / pref_w, 3) if pref_w else 0.0,
        "education_factor": edu_factor,
        "experience_factor": exp_factor,
        "must_have_factor": must_factor,
        "verified_match_count": float(len(verified)),
        "discarded_match_count": float(len(discarded)),
    }

    return ScoreResult(
        candidate_id=candidate.candidate_id,
        job_id=job.job_id,
        fit_score=fit,
        grade=grade,
        confidence=conf,
        subscores=subscores,
        explanation=explanation,
        verified_matches=verified,
        discarded_matches=discarded,
        gaps=extraction.gaps,
        rationale=extraction.rationale,
        flags=sorted(set(flags)),
    )
