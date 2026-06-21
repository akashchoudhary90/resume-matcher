"""Shared contracts — the single source of truth for every data shape that crosses a module
boundary. The LLM-facing object is `MatchExtraction` (what an adapter must return). The final,
user-facing object is `ScoreResult`, produced *deterministically* by matching/ranker.py — the LLM
never decides the score (privilege separation, see the plan's anti-injection section).

`write_json_schema()` dumps the MatchExtraction JSON Schema to disk; adapters validate LLM output
against it and CI pins it.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------------------
class Importance(str, Enum):
    essential = "essential"
    important = "important"
    optional = "optional"


class MatchStatus(str, Enum):
    match = "match"
    partial = "partial"
    missing = "missing"


class Difficulty(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------
class JobSpec(BaseModel):
    """A job posting. `required_skills` / `preferred_skills` hold canonical skill IDs
    (see matching/taxonomy.py) so synonyms collapse and keyword-stuffing gains nothing."""

    job_id: str
    title: str
    employer: str
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)  # deal-breakers: weigh 2x + gate the score
    min_education: str | None = None  # e.g. "bachelor"; used as a level gate, never a proxy feature
    min_years: float | None = None  # minimum years of experience the role expects (graded penalty)


class CandidateProfile(BaseModel):
    """A student profile. `text` is ALREADY PII-redacted (see inference/redaction.py) before it
    reaches any adapter. No protected attributes or deliberate proxies live here — those are kept
    in the separate audit store (stores/data_planes.py)."""

    candidate_id: str
    skills: list[str] = Field(default_factory=list)  # canonical skill IDs
    education_level: str | None = None
    years_experience: float = 0.0
    text: str = ""  # redacted resume text — the only free text an adapter sees
    has_resume: bool = True


# --------------------------------------------------------------------------------------
# LLM output — the adapter contract
# --------------------------------------------------------------------------------------
class SkillEvidence(BaseModel):
    """One skill assessed against the job. `evidence_span` MUST be a verbatim substring of the
    candidate's redacted text; the deterministic ranker discards any match whose span cannot be
    found in the source text (defeats fabricated/injected claims)."""

    skill_id: str
    skill_name: str
    status: MatchStatus
    importance: Importance = Importance.important
    evidence_span: str | None = None
    recency_years: float | None = None


class Gap(BaseModel):
    skill_id: str
    skill_name: str
    importance: Importance = Importance.important
    acquisition_difficulty: Difficulty = Difficulty.medium
    suggested_action: str = ""


class MatchExtraction(BaseModel):
    """EXACTLY what an InferenceAdapter must return. This is structured EXTRACTION only — the model
    proposes skill matches/gaps and a free-text rationale, but holds NO authority over the final
    numeric score. matching/ranker.py turns this into a ScoreResult deterministically."""

    candidate_id: str
    job_id: str
    skill_matches: list[SkillEvidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    seniority_assessment: str = ""
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Score explanation — the auditable derivation of the number (every point is traceable)
# --------------------------------------------------------------------------------------
class ScoreComponent(BaseModel):
    """One line item in the score breakdown. Every point of `fit_score` is attributed to exactly
    one of these, so a coordinator (or the candidate) can see WHY the number is what it is.

    `points_earned` is the actual contribution to the 0-100 score; `points_possible` is the most
    this skill could have contributed. `evidence_span` is the verbatim resume quote that justifies a
    match — the heart of the "valid reasoning" requirement. `verified` is False when the model
    claimed a skill but the quote could not be found in the resume (the claim is then NOT counted)."""

    skill_id: str
    skill_name: str
    bucket: str  # "required" | "preferred" | "education" | "info"
    importance: Importance = Importance.important
    status: MatchStatus = MatchStatus.missing
    verified: bool = False
    evidence_span: str | None = None  # verbatim quote from the resume that proves the match
    points_possible: float = 0.0
    points_earned: float = 0.0
    note: str = ""  # plain-English reason for this line


class ScoreExplanation(BaseModel):
    """A fully decomposed, human-readable derivation of `fit_score`. The components reconcile
    EXACTLY to the headline number:
        round(sum(c.points_earned for required+preferred) * education_factor, 1) == fit_score
    so the UI can show the arithmetic and the user can check it themselves."""

    formula: str = ""  # the literal arithmetic, e.g. "100 x (0.75 x req_cov + 0.25 x pref_cov) x edu"
    components: list[ScoreComponent] = Field(default_factory=list)
    required_possible: float = 0.0
    preferred_possible: float = 0.0
    required_earned: float = 0.0
    preferred_earned: float = 0.0
    subtotal: float = 0.0  # required_earned + preferred_earned, BEFORE the multipliers
    education_factor: float = 1.0
    education_note: str = ""
    experience_factor: float = 1.0  # graded penalty when below the job's minimum years
    experience_note: str = ""
    must_have_factor: float = 1.0  # penalty when a deal-breaker (must-have) skill is missing
    must_have_note: str = ""
    final_score: float = 0.0  # round(subtotal * education * experience * must_have, 1) == fit_score
    summary: str = ""  # one-paragraph plain-English "why this score"


# --------------------------------------------------------------------------------------
# Final, user-facing output — produced deterministically, NOT by the LLM
# --------------------------------------------------------------------------------------
class ScoreResult(BaseModel):
    """The honest deliverable. `fit_score` is a FIT/READINESS score (0-100), explicitly NOT a
    predicted probability of being hired — we lack the outcome labels for that (see plan §B).
    `subscores`, `verified_matches`, and `explanation` make every point traceable."""

    candidate_id: str
    job_id: str
    fit_score: float = Field(ge=0.0, le=100.0)
    grade: str  # "A" | "B" | "C" | "D"
    confidence: Confidence = Confidence.medium
    subscores: dict[str, float] = Field(default_factory=dict)
    explanation: ScoreExplanation | None = None  # auditable point-by-point derivation
    verified_matches: list[SkillEvidence] = Field(default_factory=list)
    discarded_matches: list[SkillEvidence] = Field(default_factory=list)  # unverifiable / flagged
    gaps: list[Gap] = Field(default_factory=list)
    rationale: str = ""
    flags: list[str] = Field(default_factory=list)  # advisory anti-gaming flags, never auto-reject

    # Honesty guardrail: this label travels with every result so a UI cannot relabel it.
    score_kind: str = "fit_readiness_not_hire_probability"


# Default on-disk location for the pinned JSON Schema (the adapter output contract).
SCHEMA_PATH = Path(__file__).with_name("match_extraction.schema.json")


def write_json_schema(path: Path | None = None) -> str:
    """Dump the MatchExtraction JSON Schema to disk and return the path. Pin this in CI so a
    breaking change to the adapter contract is caught."""
    path = path or SCHEMA_PATH
    schema = MatchExtraction.model_json_schema()
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return str(path)


def match_extraction_schema() -> dict:
    """Return the MatchExtraction JSON Schema as a dict (used by adapters to constrain output)."""
    return MatchExtraction.model_json_schema()
