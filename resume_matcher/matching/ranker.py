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
from .taxonomy import are_related, canonical_name, normalize_skills, surface_forms

_EDU_RANK = {
    "highschool": 0, "high school": 0, "diploma": 1, "certificate": 1, "associate": 2,
    "bachelor": 3, "bachelors": 3, "master": 4, "masters": 4, "phd": 5, "doctorate": 5,
}

_STATUS_WEIGHT = {MatchStatus.match: 1.0, MatchStatus.partial: 0.5, MatchStatus.missing: 0.0}

_REQUIRED_WEIGHT = 75.0
_PREFERRED_WEIGHT = 25.0
_BELOW_EDU_FACTOR = 0.85
_MUST_HAVE_WEIGHT = 2.0          # a must-have skill weighs 2x a regular required skill
_MISSING_MUST_HAVE_FACTOR = 0.4  # floor: ALL must-haves missing (heavy penalty, never auto-rejects)
_MISSING_MUST_HAVE_CAP = 0.7     # most relief when only a few of several must-haves are missing
_MIN_EXPERIENCE_FACTOR = 0.7     # floor of the graded experience penalty
_MAX_EVIDENCE_SPAN = 160
_MIN_EVIDENCE_ALNUM = 3          # a quote with fewer alphanumerics is too generic to be evidence

# Integrity (anti-gaming) penalties. Advisory flags from antigaming/* now DOWN-WEIGHT the score via a
# bounded multiplier — they never auto-reject (locked decision). Each tuple: (flag-prefix, factor,
# human label). The verbatim-evidence check already blocks fabricated *skills*; this additionally
# discounts resumes that game the screener (e.g. a keyword-stuffed resume that used to score ~94).
_INTEGRITY_PENALTIES = [
    ("stuffing:repetition", 0.7, "keyword stuffing (a term spammed to game matching)"),
    ("stuffing:jd_echo", 0.85, "pastes long verbatim runs of the job description"),
    ("injection:phrase", 0.7, "text attempting to manipulate an automated screener"),
    ("injection:zero_width", 0.7, "hidden / zero-width characters used to smuggle text"),
    ("hidden_text:white_text", 0.7, "hidden white-on-white text (invisible keyword stuffing)"),
    ("hidden_text:tiny_font", 0.7, "near-invisible tiny-font text"),
    # High-precision (>=12 distinct hidden-only tokens): near-white / CMYK-white / off-canvas text
    # a reader would never see — the carrier class that exact-white detection missed.
    ("hidden_text:invisible_layer", 0.7, "an invisible text layer (near-white or off-page keyword carrier)"),
]
_INTEGRITY_FLOOR = 0.5           # combined integrity penalty never drops below this (no auto-reject)

_WS_RE = re.compile(r"\s+")
# Unicode-aware: a CJK/Cyrillic demonstrating quote must keep its residue, or "用 MySQL 优化了订单查询性能"
# would strip to just "mysql" and be mis-clamped as a bare mention.
_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)


def _alnum(s: str) -> str:
    return _ALNUM_RE.sub("", (s or "").lower())


# Filler words that name-drop a skill without demonstrating it ("Skills:", "proficient in ...").
_MENTION_FILLER = {
    "skills", "skill", "technologies", "technology", "tools", "tool", "stack", "and", "in", "with",
    "of", "using", "other", "plus", "etc", "proficient", "proficiency", "familiar", "familiarity",
    "knowledge", "experienced", "expert", "advanced", "intermediate", "basic", "working",
}
_MENTION_TOKEN_RE = re.compile(r"[\w+#.]+", re.UNICODE)


def _surface_present(span: str, skill_id: str) -> bool:
    """Does the skill's own name/alias appear in `span`, word-bounded? Complements
    normalize_skills for ids its precision guards exclude (one-letter 'r', stopworded 'go'):
    those must still be attributable when they are plainly the quoted name. The extra &- guards
    keep one-letter forms from matching inside 'R&D' / 'R-squared'."""
    for form in surface_forms(skill_id):
        if not form:
            continue
        pat = r"(?<![\w+#.&-])" + re.escape(form) + r"(?![\w+#&-])(?!\.\w)"
        if re.search(pat, span, re.IGNORECASE):
            return True
    return False


def _is_bare_mention(span: str | None, skill_id: str) -> bool:
    """True when the evidence quote NAMES the skill without showing its USE — the carrier of
    skills-list dumps. Detectors, all working on the verified quote:
      (1) the quote is essentially just the skill's own name;
      (2) after stripping every recognized skill name (trailing sentence punctuation tolerated) and
          naming-filler ("Skills:", "proficient in"), the ALPHANUMERIC residue is negligible;
      (3) list context: two or more skill names in comma-list shape, where the residue amounts to
          at most a junk word per name ("Python ninja, MySQL guru") — decoration is not use.
    A quote with real demonstration ("Tuned MySQL replication for the events app") keeps plenty of
    residue and is untouched. The extraction prompt demands the demonstrating phrase for a 'match',
    so a name-only quote means there was nothing better to quote."""
    span = span or ""
    span_a = _alnum(span)
    if not span_a:
        return True
    for form in surface_forms(skill_id):
        f_a = _alnum(form)
        if f_a and f_a in span_a and len(span_a) <= len(f_a) + 3:
            return True
    found = set(normalize_skills(span))
    if skill_id not in found:
        if not _surface_present(span, skill_id):
            return False  # can't attribute the quote's names — stay conservative
        found.add(skill_id)  # precision-guarded id (e.g. 'r'): its own surface is plainly there
    seqs = sorted(
        {tuple(_MENTION_TOKEN_RE.findall(f.lower())) for fid in found for f in surface_forms(fid)},
        key=len, reverse=True)
    # Trailing sentence punctuation must not break sequence matching ('aws.' at end of sentence).
    tokens = [t if t.rstrip(".") == "" else t.rstrip(".")
              for t in _MENTION_TOKEN_RE.findall(span.lower())]
    residue: list[str] = []
    n_seq = 0
    i = 0
    while i < len(tokens):
        for s in seqs:
            if s and tuple(tokens[i:i + len(s)]) == s:
                i += len(s)
                n_seq += 1
                break
        else:
            residue.append(tokens[i])
            i += 1
    leftover = _alnum("".join(t for t in residue if t not in _MENTION_FILLER))
    if len(leftover) <= 3:
        return True
    # List context: N skill names with list punctuation and only ~a junk word of residue per name.
    if n_seq >= 2 and span.count(",") >= n_seq - 1 and len(leftover) <= 5 * n_seq:
        return True
    return False


def _integrity_factor(flags: list[str]) -> tuple[float, str]:
    """Bounded down-weight for anti-gaming signals present in `flags`. Returns (factor, note)."""
    factor, hits = 1.0, []
    for prefix, mult, label in _INTEGRITY_PENALTIES:
        if any(f.startswith(prefix) for f in flags):
            factor *= mult
            hits.append(label)
    if not hits:
        return 1.0, ""
    factor = round(max(_INTEGRITY_FLOOR, factor), 3)
    return factor, f"Integrity signals ({'; '.join(hits)}); x{factor} (flagged for human review)."


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


def _partial_note(ev: SkillEvidence, reason: str | None) -> str:
    if reason == "adjacent" and ev.adjacent_to:
        return (f"Adjacent skill demonstrated ({canonical_name(ev.adjacent_to)}) — accepted at half "
                f"weight (curated adjacency; close transfer a recruiter would credit).")
    if reason == "adjacent_bare" and ev.adjacent_to:
        return (f"Adjacent skill ({canonical_name(ev.adjacent_to)}) is NAMED in the resume but not "
                f"demonstrated — counted at half weight and flagged for review.")
    if reason == "bare_mention":
        return ("Skill is named in the resume but the quote shows no actual use of it — counted at "
                "half weight (named is not demonstrated).")
    return "Partially evidenced — counted at half weight."


def _weighted_components(
    skill_ids: list[str],
    bucket_name: str,
    bucket_total: float,
    weight_of: dict[str, float],
    importance_of: dict[str, Importance],
    verified_by_skill: dict[str, SkillEvidence],
    discarded_by_skill: dict[str, SkillEvidence],
    partial_reason: dict[str, str | None],
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
                else _partial_note(ev, partial_reason.get(sid))
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


def _summary(fit, grade, comps, n_discarded, missing_must, edu_factor, exp_factor,
             integrity_factor=1.0, n_adjacent=0, n_bare=0) -> str:
    real = [c for c in comps if c.bucket in ("required", "preferred")]
    got = sum(1 for c in real if c.verified and c.status == MatchStatus.match)
    parts = [f"Fit {fit:.1f} (grade {grade})."]
    if real:
        parts.append(f"Matched {got} of {len(real)} listed skills with verbatim evidence.")
    if n_adjacent:
        parts.append(f"{n_adjacent} skill(s) credited via a demonstrated ADJACENT skill (half weight).")
    if n_bare:
        parts.append(f"{n_bare} skill(s) only NAMED, not demonstrated — counted at half weight.")
    if missing_must:
        parts.append(f"Missing must-have skill(s): {', '.join(canonical_name(s) for s in missing_must)} "
                     f"— heavily penalized.")
    if n_discarded:
        parts.append(f"{n_discarded} claimed skill(s) could not be verified and were not counted.")
    if exp_factor < 1.0:
        parts.append("Below the job's minimum experience (penalty applied).")
    if edu_factor < 1.0:
        parts.append("Below the job's minimum education (penalty applied).")
    if integrity_factor < 1.0:
        parts.append("Anti-gaming signals detected (keyword stuffing / injection) — score down-weighted, "
                     "flagged for human review.")
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

    verified: list[tuple[SkillEvidence, str | None]] = []  # (evidence, partial-credit reason)
    discarded: list[SkillEvidence] = []
    cand_text_norm = _norm_ws(candidate.text).lower()
    for ev in extraction.skill_matches:
        if ev.status == MatchStatus.missing or ev.skill_id not in job_skill_ids:
            continue  # off-spec or non-match -> never counted
        if not _verify(cand_text_norm, ev):
            discarded.append(ev)
            flags.append(f"unverifiable_evidence:{ev.skill_id}")
            continue
        reason = None
        if ev.adjacent_to:
            # ADJACENCY: the model proposes, deterministic checks decide. Three gates:
            # (1) the relation must be in the curated graph (the LLM cannot invent relatedness);
            # (2) the quote must actually contain/evidence the ADJACENT skill — otherwise any
            #     verbatim sentence ("worked as a barista") could smuggle in adjacency credit;
            # (3) a quote that merely NAMES the adjacent skill is treated exactly like a direct
            #     bare mention: still half credit, but flagged and honestly worded.
            if not are_related(ev.skill_id, ev.adjacent_to):
                discarded.append(ev)
                flags.append(f"invalid_adjacency:{ev.skill_id}")
                continue
            span_ids = set(normalize_skills(ev.evidence_span or ""))
            if ev.adjacent_to not in span_ids and not _surface_present(ev.evidence_span or "",
                                                                       ev.adjacent_to):
                discarded.append(ev)
                flags.append(f"invalid_adjacency:{ev.skill_id}")
                continue
            if ev.status != MatchStatus.partial:
                ev = ev.model_copy(update={"status": MatchStatus.partial})  # never full credit
            reason = ("adjacent_bare" if _is_bare_mention(ev.evidence_span, ev.adjacent_to)
                      else "adjacent")
        elif ev.status == MatchStatus.match and _is_bare_mention(ev.evidence_span, ev.skill_id):
            # NAMED != DEMONSTRATED: a quote that is just the skill's name proves the mention, not
            # the use — the carrier of skills-list dumps. Half credit, plainly explained.
            ev = ev.model_copy(update={"status": MatchStatus.partial})
            reason = "bare_mention"
        verified.append((ev, reason))

    # Order-independent dedupe: strongest status wins; on equal status the most creditable REASON
    # wins (genuine partial > demonstrated-adjacent > bare variants), then the shorter span — so
    # shuffled extractions produce identical scores, notes, and flags.
    _REASON_RANK = {None: 0, "adjacent": 1, "adjacent_bare": 2, "bare_mention": 3}
    verified_by_skill: dict[str, SkillEvidence] = {}
    partial_reason: dict[str, str | None] = {}
    for ev, reason in verified:
        prev = verified_by_skill.get(ev.skill_id)
        if prev is not None:
            flags.append(f"duplicate_skill_evidence:{ev.skill_id}")
        key = (-_STATUS_WEIGHT[ev.status], _REASON_RANK[reason], len(ev.evidence_span or ""),
               ev.evidence_span or "")
        prev_key = (None if prev is None else
                    (-_STATUS_WEIGHT[prev.status], _REASON_RANK[partial_reason.get(ev.skill_id)],
                     len(prev.evidence_span or ""), prev.evidence_span or ""))
        if prev is None or key < prev_key:
            verified_by_skill[ev.skill_id] = ev
            partial_reason[ev.skill_id] = reason
    # Flags derive from the post-dedupe WINNERS: a bare/adjacent duplicate beaten by a stronger full
    # match must not leave a stale, contradictory flag behind.
    for sid, reason in partial_reason.items():
        if reason in ("adjacent", "adjacent_bare"):
            flags.append(f"adjacent_credit:{sid}")
        if reason in ("adjacent_bare", "bare_mention"):
            flags.append(f"bare_mention:{sid}")
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
        req_ids, "required", req_w, weight_of, importance_of, verified_by_skill, discarded_by_skill,
        partial_reason)
    pref_comps, pref_earned = _weighted_components(
        pref_ids, "preferred", pref_w, weight_of, importance_of, verified_by_skill, discarded_by_skill,
        partial_reason)

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

    # Must-have gate: missing deal-breakers penalize, GRADED by the fraction missing — missing 1 of 4
    # is less severe than missing all 4 — and floored at _MISSING_MUST_HAVE_FACTOR (never auto-rejects).
    # With a single must-have, missing it still applies the full floor (backward-compatible).
    missing_must = [s for s in req_ids if s in must_set and s not in verified_by_skill]
    must_factor, must_note = 1.0, ""
    if must_set:
        if missing_must:
            # Graded between the floor (ALL missing) and the cap (a few of several missing). Even one
            # missing deal-breaker is a real penalty (factor <= cap); a single must-have missing floors
            # at _MISSING_MUST_HAVE_FACTOR (backward-compatible). frac_missing in (0, 1].
            frac_missing = len(missing_must) / len(must_set)
            must_factor = round(
                _MISSING_MUST_HAVE_FACTOR
                + (_MISSING_MUST_HAVE_CAP - _MISSING_MUST_HAVE_FACTOR) * (1.0 - frac_missing), 3)
            must_note = (f"Missing {len(missing_must)} of {len(must_set)} must-have skill(s): "
                         f"{', '.join(canonical_name(s) for s in missing_must)} — x{must_factor}.")
            for s in missing_must:
                flags.append(f"missing_must_have:{s}")
        else:
            must_note = "All must-have skills are present (no adjustment)."

    # Integrity gate: anti-gaming signals (keyword stuffing, JD echo, prompt-injection text) apply a
    # bounded down-weight. Never an auto-reject — the resume stays listed and the signal is flagged.
    integrity_factor, integrity_note = _integrity_factor(flags)

    fit = round(subtotal * edu_factor * exp_factor * must_factor * integrity_factor, 1)
    grade = "A" if fit >= 80 else "B" if fit >= 65 else "C" if fit >= 50 else "D"

    info_comps: list[ScoreComponent] = []
    if not has_req and not has_pref:
        info_comps.append(ScoreComponent(
            skill_id="-", skill_name="(no skills provided)", bucket="info",
            status=MatchStatus.missing, verified=False, points_possible=0.0, points_earned=0.0,
            note="No required or preferred skills were provided, so no fit score can be computed."))

    explanation = ScoreExplanation(
        formula="fit = round( skills_subtotal x education x experience x must_have x integrity , 1 )",
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
        integrity_factor=integrity_factor,
        integrity_note=integrity_note,
        final_score=fit,
        summary=_summary(fit, grade, req_comps + pref_comps, len(discarded), missing_must,
                         edu_factor, exp_factor, integrity_factor,
                         n_adjacent=sum(1 for r in partial_reason.values()
                                        if r in ("adjacent", "adjacent_bare")),
                         n_bare=sum(1 for r in partial_reason.values()
                                    if r in ("bare_mention", "adjacent_bare"))),
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
        verified_matches=[ev for ev, _reason in verified],
        discarded_matches=discarded,
        gaps=extraction.gaps,
        rationale=extraction.rationale,
        flags=sorted(set(flags)),
    )
