"""Deterministic ranker — where the scoring DECISION is made (never by the LLM).

It takes the LLM's MatchExtraction and:
  1. VERIFIES every claimed evidence span is a real verbatim substring of the candidate's text.
     Unverifiable matches are discarded and flagged — this defeats fabricated/injected skill claims.
  2. Computes an explainable fit/readiness score from coverage of essential vs preferred skills,
     with an education-level gate. The number is reproducible and decomposed into subscores AND a
     point-by-point `ScoreExplanation` whose line items reconcile EXACTLY to `fit_score` — so the
     coordinator (and the candidate) can see why the number is what it is.

This is the honesty boundary: `fit_score` is fit/readiness, NOT a predicted probability of hire.
"""
from __future__ import annotations

from ..inference.schema import (
    CandidateProfile,
    Confidence,
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
    "highschool": 0,
    "high school": 0,
    "diploma": 1,
    "certificate": 1,
    "associate": 2,
    "bachelor": 3,
    "bachelors": 3,
    "master": 4,
    "masters": 4,
    "phd": 5,
    "doctorate": 5,
}

_STATUS_WEIGHT = {MatchStatus.match: 1.0, MatchStatus.partial: 0.5, MatchStatus.missing: 0.0}

# The score is 100 points split between the two skill buckets. Required dominates (3:1).
_REQUIRED_WEIGHT = 75.0
_PREFERRED_WEIGHT = 25.0
_BELOW_EDU_FACTOR = 0.85
_MAX_EVIDENCE_SPAN = 160  # chars; the schema promises a SHORT quote (bounds retained verbatim text)


def _edu_rank(level: str | None) -> int | None:
    if not level:
        return None
    return _EDU_RANK.get(level.strip().lower())


def _verify(text: str, ev: SkillEvidence) -> bool:
    """A match/partial is verified only if its evidence span is genuinely present in the text."""
    if ev.status == MatchStatus.missing:
        return False
    if not ev.evidence_span:
        return False
    return ev.evidence_span.lower() in text.lower()


def _clip_span(span: str | None) -> str | None:
    """Bound a retained evidence quote to a short snippet. The full span is still used for the
    verbatim-substring check in _verify(); we only clip what we KEEP, so a long span (possible with a
    non-mock LLM backend) cannot carry a paragraph of residual PII into the persisted breakdown."""
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


def _bucket_components(
    skill_ids: list[str],
    bucket_name: str,
    bucket_weight: float,
    verified_by_skill: dict[str, SkillEvidence],
    discarded_by_skill: dict[str, SkillEvidence],
) -> tuple[list[ScoreComponent], float]:
    """Build one ScoreComponent per job skill in this bucket and return (components, earned_total).

    The DISPLAYED per-row points are allocated by a cumulative-rounding scheme so they sum EXACTLY to
    the rounded bucket totals (no per-row drift). `earned_total` is the unrounded sum, used for the
    headline coverage math. An empty bucket contributes nothing (its weight is redistributed to the
    other bucket by the caller — we never award free points for skills the job didn't list)."""
    if not skill_ids or bucket_weight <= 0:
        return [], 0.0

    per = bucket_weight / len(skill_ids)
    comps: list[ScoreComponent] = []
    earned_total = 0.0
    cum_poss_raw = cum_poss_alloc = 0.0
    cum_earn_raw = cum_earn_alloc = 0.0

    for sid in skill_ids:
        ev = verified_by_skill.get(sid)
        weight = _STATUS_WEIGHT[ev.status] if ev is not None else 0.0
        earned_total += per * weight

        # Cumulative allocation: each row gets the delta of the rounded running total, so the rows
        # provably sum to round(bucket_weight) / round(earned_total).
        cum_poss_raw += per
        row_possible = round(round(cum_poss_raw, 2) - cum_poss_alloc, 2)
        cum_poss_alloc = round(cum_poss_alloc + row_possible, 2)
        cum_earn_raw += per * weight
        row_earned = round(round(cum_earn_raw, 2) - cum_earn_alloc, 2)
        cum_earn_alloc = round(cum_earn_alloc + row_earned, 2)

        if ev is not None:
            note = (
                "Found in the resume — quote verified verbatim."
                if ev.status == MatchStatus.match
                else "Partially evidenced — counted at half weight."
            )
            comps.append(
                ScoreComponent(
                    skill_id=sid,
                    skill_name=canonical_name(sid),
                    bucket=bucket_name,
                    importance=ev.importance,
                    status=ev.status,
                    verified=True,
                    evidence_span=_clip_span(ev.evidence_span),
                    points_possible=row_possible,
                    points_earned=row_earned,
                    note=note,
                )
            )
            continue

        dev = discarded_by_skill.get(sid)
        if dev is not None:
            comps.append(
                ScoreComponent(
                    skill_id=sid,
                    skill_name=canonical_name(sid),
                    bucket=bucket_name,
                    importance=dev.importance,
                    status=dev.status,
                    verified=False,
                    evidence_span=None,  # ungrounded model text — not a resume substring; never retained
                    points_possible=row_possible,
                    points_earned=0.0,
                    note=(
                        "Claimed, but the supporting quote could not be found verbatim in the "
                        "resume — NOT counted (anti-fabrication / anti-injection safeguard)."
                    ),
                )
            )
            continue

        comps.append(
            ScoreComponent(
                skill_id=sid,
                skill_name=canonical_name(sid),
                bucket=bucket_name,
                status=MatchStatus.missing,
                verified=False,
                points_possible=row_possible,
                points_earned=0.0,
                note="Not found in the resume.",
            )
        )
    return comps, earned_total


def _summary(
    fit: float,
    grade: str,
    req_comps: list[ScoreComponent],
    pref_comps: list[ScoreComponent],
    n_discarded: int,
    edu_factor: float,
    edu_note: str,
) -> str:
    def counts(comps: list[ScoreComponent]) -> tuple[int, int]:
        real = [c for c in comps if c.bucket != "info"]
        got = sum(1 for c in real if c.verified and c.status == MatchStatus.match)
        return got, len(real)

    rgot, rtot = counts(req_comps)
    pgot, ptot = counts(pref_comps)
    parts = [f"Fit {fit:.1f} (grade {grade})."]
    if rtot:
        parts.append(f"Matched {rgot} of {rtot} required skills with verbatim evidence.")
    if ptot:
        parts.append(f"Matched {pgot} of {ptot} preferred skills.")
    if n_discarded:
        parts.append(
            f"{n_discarded} claimed skill(s) could not be verified against the resume and were not counted."
        )
    if edu_factor < 1.0:
        parts.append(edu_note)
    return " ".join(parts)


def score(
    extraction: MatchExtraction,
    candidate: CandidateProfile,
    job: JobSpec,
    extra_flags: list[str] | None = None,
) -> ScoreResult:
    flags = list(extra_flags or [])

    # De-duplicate job skills (a malformed jobs.csv can repeat a skill or list it in both buckets);
    # a skill is counted once, as required if it appears there. JobSpec carries no uniqueness
    # guarantee, so the ranker must not assume de-duped input.
    req_ids = _dedupe(job.required_skills)
    req_set = set(req_ids)
    pref_ids = [s for s in _dedupe(job.preferred_skills) if s not in req_set]
    if len(req_ids) != len(job.required_skills) or len(pref_ids) != len(job.preferred_skills):
        flags.append("duplicate_skill_ids_collapsed")
    job_skill_ids = req_set | set(pref_ids)

    verified: list[SkillEvidence] = []
    discarded: list[SkillEvidence] = []

    for ev in extraction.skill_matches:
        if ev.status == MatchStatus.missing:
            continue
        if ev.skill_id not in job_skill_ids:
            continue  # off-spec claim: not part of this job -> never counted as verified/discarded
        if _verify(candidate.text, ev):
            verified.append(ev)
        else:
            discarded.append(ev)
            flags.append(f"unverifiable_evidence:{ev.skill_id}")

    # On duplicate evidence for one skill, keep the STRONGEST verified status (order-independent),
    # and flag the collapse so it is auditable — never let a later weaker claim silently downgrade.
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

    # Weights are renormalized so points come ONLY from skills the job actually listed:
    #   both buckets present -> 75 / 25 ; only one present -> that bucket gets the full 100.
    has_req = bool(req_ids)
    has_pref = bool(pref_ids)
    if has_req and has_pref:
        req_w, pref_w = _REQUIRED_WEIGHT, _PREFERRED_WEIGHT
    elif has_req:
        req_w, pref_w = 100.0, 0.0
    elif has_pref:
        req_w, pref_w = 0.0, 100.0
    else:
        req_w, pref_w = 0.0, 0.0  # no skills at all -> nothing to score

    # ---- the breakdown: one line item per job skill, reconciling exactly to fit_score ----------
    req_comps, req_earned = _bucket_components(
        req_ids, "required", req_w, verified_by_skill, discarded_by_skill
    )
    pref_comps, pref_earned = _bucket_components(
        pref_ids, "preferred", pref_w, verified_by_skill, discarded_by_skill
    )

    required_cov = req_earned / req_w if req_w else 0.0
    preferred_cov = pref_earned / pref_w if pref_w else 0.0
    # Use the rounded, DISPLAYED earnings so the on-screen math reconciles exactly:
    #   final == round(subtotal * education_factor, 1), and subtotal == required + preferred.
    required_earned = round(req_earned, 2)
    preferred_earned = round(pref_earned, 2)
    subtotal = round(required_earned + preferred_earned, 2)

    # Education gate: below the stated minimum applies a penalty, never an outright proxy feature.
    edu_factor = 1.0
    edu_note = "Education meets or exceeds the job's stated minimum (no adjustment)."
    need = _edu_rank(job.min_education)
    have = _edu_rank(candidate.education_level)
    if need is not None and have is not None and have < need:
        edu_factor = _BELOW_EDU_FACTOR
        edu_note = (
            f"Listed education ({candidate.education_level}) is below the job minimum "
            f"({job.min_education}); a x{_BELOW_EDU_FACTOR} penalty was applied "
            f"(self-reported, not evidence-verified)."
        )
        flags.append("below_min_education")
    elif need is None:
        edu_note = "The job specified no minimum education (no adjustment)."
    elif have is None:
        edu_note = "Education level could not be determined from the resume (no adjustment)."
    else:  # meets or exceeds the minimum
        edu_note = (
            "Education meets or exceeds the job's stated minimum "
            "(self-reported, not evidence-verified; no adjustment)."
        )

    fit = round(subtotal * edu_factor, 1)
    grade = "A" if fit >= 80 else "B" if fit >= 65 else "C" if fit >= 50 else "D"

    edu_comp = ScoreComponent(
        skill_id="-",
        skill_name="Education gate",
        bucket="education",
        status=MatchStatus.match if edu_factor == 1.0 else MatchStatus.partial,
        verified=True,
        points_possible=0.0,
        points_earned=round(fit - subtotal, 2),  # 0 or a negative adjustment; residual to the rounded fit so subtotal + adj == fit_score
        note=edu_note,
    )

    info_comps: list[ScoreComponent] = []
    if not has_req and not has_pref:
        info_comps.append(
            ScoreComponent(
                skill_id="-",
                skill_name="(no skills provided)",
                bucket="info",
                status=MatchStatus.missing,
                verified=False,
                points_possible=0.0,
                points_earned=0.0,
                note="No required or preferred skills were provided, so no fit score can be computed.",
            )
        )

    weighting = (
        f"Required worth {req_w:.0f} pts, preferred worth {pref_w:.0f} pts."
        if (has_req or has_pref)
        else "No skills were provided."
    )
    explanation = ScoreExplanation(
        formula=(
            f"{weighting} fit = round( (required_earned + preferred_earned) x education_factor , 1 )"
        ),
        components=req_comps + pref_comps + [edu_comp] + info_comps,
        required_possible=round(req_w, 1),
        preferred_possible=round(pref_w, 1),
        required_earned=required_earned,
        preferred_earned=preferred_earned,
        subtotal=subtotal,
        education_factor=edu_factor,
        education_note=edu_note,
        final_score=fit,
        summary=_summary(fit, grade, req_comps, pref_comps, len(discarded), edu_factor, edu_note),
    )

    # Confidence reflects how much we can trust the inputs, not how good the candidate is.
    if not candidate.has_resume or len(candidate.text) < 200:
        conf = Confidence.low
    elif discarded:
        conf = Confidence.medium
    else:
        conf = Confidence.high

    subscores = {
        "required_coverage": round(required_cov, 3),
        "preferred_coverage": round(preferred_cov, 3),
        "education_factor": edu_factor,
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
