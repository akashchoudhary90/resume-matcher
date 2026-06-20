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
    min_education: str | None = None  # e.g. "bachelor"; used as a level gate, never a proxy feature


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
# Final, user-facing output — produced deterministically, NOT by the LLM
# --------------------------------------------------------------------------------------
class ScoreResult(BaseModel):
    """The honest deliverable. `fit_score` is a FIT/READINESS score (0-100), explicitly NOT a
    predicted probability of being hired — we lack the outcome labels for that (see plan §B).
    `subscores` and `verified_matches` make every point traceable."""

    candidate_id: str
    job_id: str
    fit_score: float = Field(ge=0.0, le=100.0)
    grade: str  # "A" | "B" | "C" | "D"
    confidence: Confidence = Confidence.medium
    subscores: dict[str, float] = Field(default_factory=dict)
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
