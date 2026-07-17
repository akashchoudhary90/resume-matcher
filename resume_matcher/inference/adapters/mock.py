"""Deterministic, dependency-free adapter. Default in CI and tests.

Because it is pure code (no model), it is structurally immune to prompt injection: hidden text in a
resume cannot change its behavior. It produces schema-valid MatchExtraction output suitable for the
contract test, and realistic enough to drive the full pipeline on synthetic data.
"""
from __future__ import annotations

from ..schema import (
    CandidateProfile,
    Difficulty,
    Gap,
    Importance,
    JobSpec,
    MatchExtraction,
    MatchStatus,
    SkillEvidence,
)
import re

from ..adapter import InferenceAdapter
from ...matching.taxonomy import canonical_name, related_skills, surface_forms

_SPAN_CONTEXT = 40  # chars of surrounding context quoted with the skill name


def _find_span(text: str, skill_id: str) -> str | None:
    """A verbatim quote around the skill mention, WITH surrounding context — like the real engine is
    prompted to do ("quote the phrase that demonstrates the skill"). Quoting only the bare name
    would (correctly) be down-graded by the ranker's named-is-not-demonstrated check.

    Matches use the SAME word boundaries as the taxonomy scanner — a naive substring search let the
    one-letter skill "R" match inside "developer", which surfaced as phantom adjacency credit.

    Surfaces come from the taxonomy's own alias-inclusive list (the one the ranker's bare-mention
    check uses), not a canonical-name-only guess: a resume writing "JS" or "Postgres" demonstrates
    the skill, and scoring it as a gap made the mock's floor a property of our synonym list rather
    than of the candidate."""
    for form in surface_forms(skill_id):
        # Skip one-letter surfaces (the taxonomy scanner's own precision guard): even with word
        # boundaries, "R" matches inside "R&D" / "R-squared" and poisons adjacency proposals.
        if not form or len(re.sub(r"[\W_]+", "", form)) < 2:
            continue
        m = re.search(r"(?<![\w+#.])" + re.escape(form) + r"(?![\w+#])(?!\.\w)", text, re.IGNORECASE)
        if m:
            start = max(0, m.start() - _SPAN_CONTEXT)
            end = min(len(text), m.end() + _SPAN_CONTEXT)
            return text[start:end].strip()
    return None


def _name(skill_id: str) -> str:
    return canonical_name(skill_id)


class MockAdapter(InferenceAdapter):
    name = "mock"
    is_local = True

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        matches: list[SkillEvidence] = []
        gaps: list[Gap] = []
        known = {s.lower() for s in candidate.skills}

        # Same merged view the ranker scores: a skill listed ONLY in must_have_skills (raw JobSpec,
        # no build_job_spec fold) must still be assessed here.
        required = list(dict.fromkeys(list(job.required_skills) + list(job.must_have_skills)))
        groups = [
            (required, Importance.essential, Difficulty.high),
            ([s for s in job.preferred_skills if s not in set(required)],
             Importance.optional, Difficulty.medium),
        ]
        for skills, importance, difficulty in groups:
            for skill_id in skills:
                span = _find_span(candidate.text, skill_id)
                if span is not None:
                    matches.append(
                        SkillEvidence(
                            skill_id=skill_id,
                            skill_name=_name(skill_id),
                            status=MatchStatus.match,
                            importance=importance,
                            evidence_span=span,
                            recency_years=None,
                        )
                    )
                elif skill_id.lower() in known:
                    # Claimed via taxonomy but not quotable -> partial; ranker will down-weight.
                    matches.append(
                        SkillEvidence(
                            skill_id=skill_id,
                            skill_name=_name(skill_id),
                            status=MatchStatus.partial,
                            importance=importance,
                            evidence_span=None,
                        )
                    )
                elif (adj := next(((rel, s) for rel in related_skills(skill_id)
                                   if (s := _find_span(candidate.text, rel))), None)):
                    # ADJACENT skill found instead (PostgreSQL for a MySQL job, ...): propose it at
                    # partial; the ranker validates the relation against the curated graph.
                    rel, span = adj
                    matches.append(
                        SkillEvidence(
                            skill_id=skill_id,
                            skill_name=_name(skill_id),
                            status=MatchStatus.partial,
                            importance=importance,
                            evidence_span=span,
                            adjacent_to=rel,
                        )
                    )
                else:
                    gaps.append(
                        Gap(
                            skill_id=skill_id,
                            skill_name=_name(skill_id),
                            importance=importance,
                            acquisition_difficulty=difficulty,
                            suggested_action=f"Build and document a project using {_name(skill_id)}.",
                        )
                    )

        yrs = candidate.years_experience
        seniority = (
            "entry-level" if yrs < 1 else "junior" if yrs < 3 else "mid-level" if yrs < 6 else "senior"
        )
        return MatchExtraction(
            candidate_id=candidate.candidate_id,
            job_id=job.job_id,
            skill_matches=matches,
            gaps=gaps,
            seniority_assessment=f"~{yrs:.1f} yrs experience ({seniority}).",
            rationale=(
                f"Matched {sum(m.status == MatchStatus.match for m in matches)} skills with quotable "
                f"evidence; {len(gaps)} gaps remain."
            ),
        )
