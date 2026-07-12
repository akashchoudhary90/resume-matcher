"""JD-autofill contracts (inference/posting_schema.py): projection into JobSpec, the boundary-#2
tripwire, and the CI schema pins."""
from __future__ import annotations

import json

import pytest

from resume_matcher.inference.posting_schema import (
    POSTING_EXTRACTION_SCHEMA_PATH,
    POSTING_SCHEMA_PATH,
    ExtractedField,
    FieldStatus,
    JobPosting,
    PostingExtraction,
    SkillDraft,
    job_posting_schema,
    posting_extraction_schema,
)
from resume_matcher.stores.data_planes import ProtectedDataError


def _draft() -> JobPosting:
    return JobPosting(
        title=ExtractedField(value="Data Analyst Intern", status=FieldStatus.auto),
        employer_name=ExtractedField(value="Acme Corp", status=FieldStatus.auto),
        description="Analyze data with Python and SQL.",
        skills=[
            SkillDraft(skill_id="python", name="Python", bucket="required"),
            SkillDraft(skill_id="sql", name="SQL", bucket="must_have"),
            SkillDraft(skill_id="tableau", name="Tableau", bucket="preferred"),
        ],
        min_education=ExtractedField(value="bachelor"),
        min_years=ExtractedField(value=2),
        work_authorization=ExtractedField(value={"statement": "Must be eligible to work in Canada"}),
        contact=ExtractedField(value={"email": "hr@acme.com"}),
    )


def test_projection_into_job_spec():
    spec = _draft().to_job_spec("J1")
    assert spec.title == "Data Analyst Intern" and spec.employer == "Acme Corp"
    assert set(spec.required_skills) == {"python", "sql"}   # must-haves fold into required
    assert spec.must_have_skills == ["sql"]
    assert spec.preferred_skills == ["tableau"]
    assert spec.min_education == "bachelor" and spec.min_years == 2.0
    # work_authorization / contact are STRUCTURALLY absent from the engine contract
    assert not hasattr(spec, "work_authorization") and not hasattr(spec, "contact")


def test_protected_term_tripwire_fires():
    draft = _draft()
    draft.skills.append(SkillDraft(skill_id="citizenship", name="Citizenship", bucket="required"))
    with pytest.raises(ProtectedDataError):
        draft.to_job_spec()


def test_implausible_years_and_education_are_dropped():
    draft = _draft()
    draft.min_years = ExtractedField(value=200)
    draft.min_education = ExtractedField(value="wizardry")
    spec = draft.to_job_spec()
    assert spec.min_years is None and spec.min_education is None


def test_posting_extraction_wire_shape_is_quote_based():
    ext = PostingExtraction.model_validate(
        {"title": {"value": "Dev", "quote": "We need a Dev"},
         "skills": [{"name": "Python", "bucket": "required", "kind": "demonstrated",
                     "quote": "build pipelines in Python"}]}
    )
    assert ext.title.quote == "We need a Dev"
    assert ext.skills[0].kind == "demonstrated"
    # No confidence/status/skill_id fields exist on the wire — code computes those.
    assert "confidence" not in PostingExtraction.model_fields
    assert "skill_id" not in type(ext.skills[0]).model_fields


def test_pinned_schema_files_match_live_contracts():
    assert json.loads(POSTING_SCHEMA_PATH.read_text(encoding="utf-8")) == job_posting_schema()
    assert json.loads(
        POSTING_EXTRACTION_SCHEMA_PATH.read_text(encoding="utf-8")
    ) == posting_extraction_schema()
