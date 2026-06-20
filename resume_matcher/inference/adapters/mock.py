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
from ..adapter import InferenceAdapter


def _surface_forms(skill_id: str) -> list[str]:
    return [skill_id.replace("_", " "), skill_id]


def _find_span(text: str, skill_id: str) -> str | None:
    low = text.lower()
    for form in _surface_forms(skill_id):
        idx = low.find(form.lower())
        if idx != -1:
            return text[idx : idx + len(form)]
    return None


def _name(skill_id: str) -> str:
    return skill_id.replace("_", " ").title()


class MockAdapter(InferenceAdapter):
    name = "mock"
    is_local = True

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        matches: list[SkillEvidence] = []
        gaps: list[Gap] = []
        known = {s.lower() for s in candidate.skills}

        groups = [
            (job.required_skills, Importance.essential, Difficulty.high),
            (job.preferred_skills, Importance.optional, Difficulty.medium),
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
