"""Plain-English translations for the advisory flags raised during scoring.

The raw flag strings (e.g. ``unverifiable_evidence:python``, ``injection:phrase:ignore...``) are
compact and machine-friendly but opaque to a coordinator. This turns each into a human sentence so
the UI can show *why* a flag fired. Flags are ALWAYS advisory — they are surfaced for human review,
never used to auto-reject a candidate (plan §D). Keeping the mapping here means both the API and any
future client render identical, vetted wording.
"""
from __future__ import annotations

# Matched by longest-prefix so "injection:zero_width" wins over "injection".
_PREFIX_TEXT: list[tuple[str, str]] = [
    (
        "unverifiable_evidence",
        "A skill was claimed but its supporting quote could not be found verbatim in the resume, "
        "so it was NOT counted toward the score (anti-fabrication / anti-injection safeguard).",
    ),
    (
        "below_min_education",
        "The candidate's listed education is below the job's stated minimum, so a small fixed "
        "penalty (x0.85) was applied to the score.",
    ),
    (
        "missing_must_have",
        "A MUST-HAVE (deal-breaker) skill for this job is missing from the resume, so the score was "
        "heavily penalized. The candidate is still listed for human review, never auto-rejected.",
    ),
    (
        "below_min_experience",
        "The candidate has fewer years of experience than the job's stated minimum, so a graded "
        "penalty was applied (self-reported, not evidence-verified).",
    ),
    (
        "no_required_skills",
        "No required skills were provided for this job. Preferred skills (if any) carried the full "
        "100 points; otherwise no score could be computed. Add required skills for a meaningful "
        "comparison.",
    ),
    (
        "duplicate_skill_ids_collapsed",
        "The job listed the same skill more than once (or in both required and preferred); it was "
        "counted only once so the score isn't inflated.",
    ),
    (
        "duplicate_skill_evidence",
        "The same skill was evidenced more than once; the strongest verified match was kept so "
        "scoring is order-independent.",
    ),
    (
        "injection:zero_width",
        "The resume contains invisible / zero-width characters sometimes used to smuggle hidden "
        "instructions past a human reader. Flagged for review; it cannot change the score.",
    ),
    (
        "injection:phrase",
        "The resume contains text that reads like an instruction to the AI (e.g. 'ignore previous "
        "instructions' / 'award full marks'). It is ignored by design and flagged for review.",
    ),
    (
        "injection",
        "The resume contains a possible prompt-injection attempt. It is ignored by design and "
        "flagged for human review; it cannot move the score.",
    ),
    (
        "stuffing:repetition",
        "A term is repeated unusually often — possible keyword stuffing to game matching. "
        "Flagged for review; repetition does not add points (skills are matched once).",
    ),
    (
        "stuffing:jd_echo",
        "A long passage of the job description appears verbatim in the resume — possible copy-paste "
        "to game keyword matching. Flagged for review.",
    ),
    (
        "stuffing",
        "Possible keyword stuffing detected. Flagged for human review; it does not add points.",
    ),
    (
        "hidden_text",
        "The document contains hidden or near-invisible text. Flagged for human review.",
    ),
]


def humanize_flag(flag: str) -> str:
    """Return a plain-English explanation for a single advisory flag string."""
    for prefix, text in _PREFIX_TEXT:
        if flag.startswith(prefix):
            return text
    return f"Advisory flag for human review: {flag}"


def humanize_flags(flags: list[str]) -> list[dict]:
    """Return ``[{flag, severity, text}, ...]`` for a list of raw flags (de-duplicated, ordered)."""
    out: list[dict] = []
    for f in flags:
        out.append({"flag": f, "severity": flag_severity(f), "text": humanize_flag(f)})
    return out


def flag_severity(flag: str) -> str:
    """'bad' (security / fabrication), 'warn' (advisory), or 'info'. Drives UI colour only."""
    if flag.startswith(("injection", "unverifiable_evidence", "hidden_text", "missing_must_have")):
        return "bad"
    if flag.startswith(("stuffing", "below_min_education", "below_min_experience", "no_required_skills")):
        return "warn"
    return "info"
