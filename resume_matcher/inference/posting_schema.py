"""JD-autofill contracts (docs/JD_AUTOFILL.md §1): the JobPosting draft served to the review UI,
the ExtractedField provenance envelope, and PostingExtraction — the LLM wire shape.

Three shapes, three authorities:
  * `PostingExtraction` — what the LLM RETURNS: values + verbatim quotes, never taxonomy IDs and
    never confidences (confidence is computed by code in ingestion/jd_merge.py).
  * `JobPosting` — the reviewable DRAFT: every extractable field wrapped in `ExtractedField`
    {value, source_span, method, confidence, status} so the review UI can show provenance and the
    publish gate can enforce the policy table.
  * `JobPosting.to_job_spec()` — the projection into the matching engine's untouched `JobSpec`
    contract. work_authorization and contact are STRUCTURALLY excluded (display-only; boundary #2),
    and a PROTECTED_KEYS tripwire rejects any protected term that sneaks into the projected fields.

Both wire schemas are dumped to disk and CI-pinned exactly like match_extraction.schema.json.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .schema import Confidence, JobSpec

# Controlled vocabularies shared by the deterministic extractors, the LLM prompt, and the merge.
WORK_MODES = ("onsite", "hybrid", "remote")
EMPLOYMENT_TYPES = ("full_time", "part_time", "internship", "co_op", "contract",
                    "new_grad", "work_study")
PAY_PERIODS = ("hour", "week", "month", "year", "stipend", "unpaid")
EDU_LEVELS = ("highschool", "diploma", "associate", "bachelor", "master", "phd")
SKILL_BUCKETS = ("must_have", "required", "preferred")

# Terms that must never appear in anything projected toward scoring (boundary #2 tripwire — the
# full proxy list lives in stores/data_planes.py; imported lazily in to_job_spec to avoid cycles).


class Method(str, Enum):
    regex = "regex"
    taxonomy = "taxonomy"
    heuristic = "heuristic"
    llm = "llm"
    merged = "merged"
    user = "user"


class FieldStatus(str, Enum):
    auto = "auto"                  # green: pre-filled, no interaction required
    needs_review = "needs_review"  # amber: publish blocked until confirmed/edited
    conflict = "conflict"          # red: two candidates / unverified — explicit decision
    missing = "missing"            # grey: nothing extracted


class ExtractedField(BaseModel):
    """The provenance envelope every draft field travels in (the design's spine)."""

    value: Any = None
    source_span: tuple[int, int] | None = None  # char offsets into the canonical JD text
    source_page: int | None = None
    method: Method | None = None
    confidence: Confidence | None = None
    status: FieldStatus = FieldStatus.missing
    candidates: list[dict] = Field(default_factory=list)  # conflict alternatives, both shown


class SkillDraft(BaseModel):
    """One taxonomy-resolved skill chip with its own provenance (skills merge per-skill)."""

    skill_id: str
    name: str = ""
    bucket: str = "required"          # must_have | required | preferred
    kind: str = "named"               # named | demonstrated (mirrors the eval grading)
    method: Method = Method.taxonomy
    confidence: Confidence = Confidence.medium
    status: FieldStatus = FieldStatus.needs_review
    source_span: tuple[int, int] | None = None


class JobPosting(BaseModel):
    """The reviewable draft. `description` is the canonical (hygiene-passed) posting text itself —
    spans index into it. Everything else is enveloped."""

    posting_id: str = ""
    title: ExtractedField = Field(default_factory=ExtractedField)
    employer_name: ExtractedField = Field(default_factory=ExtractedField)
    employer_website: ExtractedField = Field(default_factory=ExtractedField)
    locations: ExtractedField = Field(default_factory=ExtractedField)      # value: list[str]
    work_mode: ExtractedField = Field(default_factory=ExtractedField)      # value in WORK_MODES
    employment_type: ExtractedField = Field(default_factory=ExtractedField)
    pay: ExtractedField = Field(default_factory=ExtractedField)            # {min,max,currency,period}
    application_deadline: ExtractedField = Field(default_factory=ExtractedField)  # ISO date str
    start_date: ExtractedField = Field(default_factory=ExtractedField)
    description: str = ""
    responsibilities: ExtractedField = Field(default_factory=ExtractedField)      # value: list[str]
    qualifications_required: ExtractedField = Field(default_factory=ExtractedField)
    qualifications_preferred: ExtractedField = Field(default_factory=ExtractedField)
    skills: list[SkillDraft] = Field(default_factory=list)
    min_education: ExtractedField = Field(default_factory=ExtractedField)  # value in EDU_LEVELS
    min_years: ExtractedField = Field(default_factory=ExtractedField)
    # DISPLAY-ONLY: never projected into JobSpec/scoring (international status is audit-plane).
    work_authorization: ExtractedField = Field(default_factory=ExtractedField)
    application: ExtractedField = Field(default_factory=ExtractedField)    # {method,url,email}
    contact: ExtractedField = Field(default_factory=ExtractedField)        # employer business contact
    extraction_meta: dict = Field(default_factory=dict)  # source, sha256, model, language, flags[]

    def to_job_spec(self, job_id: str = "JOB") -> JobSpec:
        """Project into the matching engine's contract. work_authorization/contact/locations/pay
        are structurally absent — the engine only sees what it scores on (boundary #2/#4)."""
        from ..stores.data_planes import PROTECTED_KEYS, ProtectedDataError

        buckets: dict[str, list[str]] = {"must_have": [], "required": [], "preferred": []}
        for s in self.skills:
            if s.bucket in buckets and s.skill_id:
                buckets[s.bucket].append(s.skill_id)
                bad = PROTECTED_KEYS & {s.skill_id.lower(), (s.name or "").lower()}
                if bad:
                    raise ProtectedDataError(
                        f"Refusing to project protected term(s) {sorted(bad)} into scoring."
                    )
        edu = self.min_education.value if self.min_education.value in EDU_LEVELS else None
        years = self.min_years.value
        years = float(years) if isinstance(years, (int, float)) and 0 <= float(years) <= 30 else None
        return JobSpec(
            job_id=job_id or "JOB",
            title=str(self.title.value or "").strip() or "Untitled role",
            employer=str(self.employer_name.value or "").strip() or "Unspecified employer",
            description=self.description,
            required_skills=buckets["required"] + buckets["must_have"],
            preferred_skills=buckets["preferred"],
            must_have_skills=buckets["must_have"],
            min_education=edu,
            min_years=years,
        )


# --------------------------------------------------------------------------------------
# PostingExtraction — the LLM wire shape (values + verbatim quotes; code computes the rest)
# --------------------------------------------------------------------------------------
class QuotedStr(BaseModel):
    value: str = ""
    quote: str | None = None  # the exact source sentence; jd_merge verifies it verbatim


class PayExtraction(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str | None = None   # ISO-4217, e.g. CAD
    period: str | None = None     # one of PAY_PERIODS
    quote: str | None = None


class LLMSkill(BaseModel):
    name: str
    bucket: str = "required"      # must_have | required | preferred
    kind: str = "named"           # named literally vs demonstrated (implied by duties)
    quote: str | None = None      # REQUIRED for 'demonstrated' skills to earn any trust


class PostingExtraction(BaseModel):
    """EXACTLY what the LLM posting pass returns (adapters validate against the pinned schema).
    Names and quotes only — never taxonomy IDs, never confidences (jd_merge.py computes those)."""

    title: QuotedStr | None = None
    employer_name: QuotedStr | None = None
    employer_website: str | None = None
    locations: list[QuotedStr] = Field(default_factory=list)   # "Toronto, ON" display strings
    work_mode: str | None = None
    employment_type: str | None = None
    pay: PayExtraction | None = None
    application_deadline: QuotedStr | None = None              # value: ISO date if stated
    start_date: QuotedStr | None = None
    responsibilities: list[str] = Field(default_factory=list)
    qualifications_required: list[str] = Field(default_factory=list)
    qualifications_preferred: list[str] = Field(default_factory=list)
    skills: list[LLMSkill] = Field(default_factory=list)
    min_education: str | None = None
    min_years: float | None = None
    work_authorization_statement: str | None = None            # verbatim eligibility sentence
    sponsorship_available: bool | None = None
    application_method: str | None = None                      # platform | external_url | email
    application_url: str | None = None
    application_email: str | None = None
    multi_role_detected: bool = False
    other_role_titles: list[str] = Field(default_factory=list)
    language: str = "en"


# --------------------------------------------------------------------------------------
# CI-pinned schema files (same pattern as match_extraction.schema.json)
# --------------------------------------------------------------------------------------
POSTING_SCHEMA_PATH = Path(__file__).with_name("job_posting.schema.json")
POSTING_EXTRACTION_SCHEMA_PATH = Path(__file__).with_name("posting_extraction.schema.json")


def job_posting_schema() -> dict:
    return JobPosting.model_json_schema()


def posting_extraction_schema() -> dict:
    return PostingExtraction.model_json_schema()


def write_posting_schemas() -> tuple[str, str]:
    """Dump both JSON Schemas to disk (pin in CI so contract drift is caught)."""
    POSTING_SCHEMA_PATH.write_text(json.dumps(job_posting_schema(), indent=2), encoding="utf-8")
    POSTING_EXTRACTION_SCHEMA_PATH.write_text(
        json.dumps(posting_extraction_schema(), indent=2), encoding="utf-8"
    )
    return str(POSTING_SCHEMA_PATH), str(POSTING_EXTRACTION_SCHEMA_PATH)
