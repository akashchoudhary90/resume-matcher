"""P5 merge (ingestion/jd_merge.py): quote verification, the merge-policy matrix, clamps,
skill dual-detector rules, and the always-confirm / injection review-forcing."""
from __future__ import annotations

import datetime as dt

from resume_matcher.inference.posting_schema import (
    ExtractedField,
    FieldStatus,
    Method,
    PayExtraction,
    PostingExtraction,
    QuotedStr,
    SkillDraft,
)
from resume_matcher.inference.schema import Confidence
from resume_matcher.ingestion.jd_merge import merge_draft, verify_span

TEXT = ("Data Analyst Intern\n\nRequirements\n- Strong Python skills are required\n"
        "- 2+ years with SQL\n\nWhat You'll Do\n- Build dashboards for the ops team\n\n"
        "Pay: $24 - $28 per hour. Apply at https://jobs.example.com/apply\n")


# ---- verify_span ---------------------------------------------------------------------------------
def test_verify_span_exact_and_normalized():
    start, end = verify_span(TEXT, "Strong Python skills are required")
    assert TEXT[start:end] == "Strong Python skills are required"
    # whitespace/case-tolerant, offsets still land on the original text
    start, end = verify_span(TEXT, "strong   python SKILLS are\nrequired")
    assert TEXT[start:end] == "Strong Python skills are required"


def test_verify_span_rejects_fabrication_and_degenerates():
    assert verify_span(TEXT, "Rust experience mandatory") is None
    assert verify_span(TEXT, "a") is None          # < 3 alnum: matches anything -> rejected
    assert verify_span(TEXT, "  -  ") is None
    assert verify_span(TEXT, None) is None


# ---- merge matrix --------------------------------------------------------------------------------
def _det_title() -> ExtractedField:
    return ExtractedField(value="Data Analyst Intern", source_span=(0, 19), method=Method.heuristic,
                          confidence=Confidence.low, status=FieldStatus.needs_review)


def test_agreement_is_high_but_always_confirm_fields_stay_amber():
    llm = PostingExtraction(title=QuotedStr(value="Data Analyst Intern",
                                            quote="Data Analyst Intern"))
    draft = merge_draft(TEXT, {"title": _det_title()}, [], llm, [])
    assert draft.title.value == "Data Analyst Intern"
    assert draft.title.confidence == Confidence.high      # both detectors agree
    assert draft.title.status == FieldStatus.needs_review  # title is on the always-confirm list


def test_disagreement_prefills_deterministic_and_flags_conflict():
    llm = PostingExtraction(title=QuotedStr(value="Junior Data Scientist", quote="nonexistent"))
    draft = merge_draft(TEXT, {"title": _det_title()}, [], llm, [])
    assert draft.title.value == "Data Analyst Intern"      # deterministic pre-fills
    assert draft.title.status == FieldStatus.conflict
    assert draft.title.candidates[0]["value"] == "Junior Data Scientist"


def test_llm_only_verified_quote_is_medium_review():
    llm = PostingExtraction(min_years=2)
    draft = merge_draft(TEXT, {}, [], llm, [])
    assert draft.min_years.value == 2.0
    assert draft.min_years.status in (FieldStatus.needs_review, FieldStatus.conflict)


def test_llm_down_keeps_deterministic_fields():
    draft = merge_draft(TEXT, {"title": _det_title()}, [], None, ["llm_unavailable"])
    assert draft.title.value == "Data Analyst Intern"
    assert draft.description == TEXT


# ---- clamps --------------------------------------------------------------------------------------
def test_pay_clamps_swap_and_sanity():
    llm = PostingExtraction(pay=PayExtraction(min=28, max=24, currency="cad", period="hour",
                                              quote="$24 - $28 per hour"))
    draft = merge_draft(TEXT, {}, [], llm, [])
    assert draft.pay.value["min"] == 24.0 and draft.pay.value["max"] == 28.0
    assert draft.pay.value["currency"] == "CAD"

    silly = PostingExtraction(pay=PayExtraction(min=500, max=500, currency="CAD", period="hour"))
    draft = merge_draft(TEXT, {}, [], silly, [])
    assert draft.pay.status == FieldStatus.conflict        # outside the hourly sanity band


def test_bad_enum_and_date_values_dropped():
    llm = PostingExtraction(work_mode="telepathic", employment_type="permanent",
                            application_deadline=QuotedStr(value="soonish"),
                            min_education="wizardry")
    draft = merge_draft(TEXT, {}, [], llm, [])
    assert draft.work_mode.value is None
    assert draft.employment_type.value == "full_time"      # synonym coerced
    assert draft.application_deadline.value is None        # unparseable date dropped
    assert draft.min_education.value is None


def test_past_deadline_demoted_not_dropped():
    past = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    llm = PostingExtraction(application_deadline=QuotedStr(value=past))
    draft = merge_draft(TEXT, {}, [], llm, [])
    assert draft.application_deadline.value == past
    assert draft.application_deadline.confidence == Confidence.low


# ---- skills --------------------------------------------------------------------------------------
def _det_skill(sid: str, bucket: str = "required") -> SkillDraft:
    return SkillDraft(skill_id=sid, name=sid, bucket=bucket, method=Method.taxonomy,
                      confidence=Confidence.medium, status=FieldStatus.needs_review)


def test_dual_detector_agreement_auto_accepts():
    llm = PostingExtraction(skills=[{"name": "Python", "bucket": "required", "kind": "named",
                                     "quote": "Strong Python skills are required"}])
    draft = merge_draft(TEXT, {}, [_det_skill("python")], llm, [])
    py = next(s for s in draft.skills if s.skill_id == "python")
    assert py.confidence == Confidence.high and py.status == FieldStatus.auto


def test_llm_only_unverified_skill_is_a_greyed_conflict_chip():
    llm = PostingExtraction(skills=[{"name": "Kubernetes", "bucket": "required",
                                     "kind": "named", "quote": "not in the text"}])
    draft = merge_draft(TEXT, {}, [], llm, [])
    k8s = next(s for s in draft.skills if s.skill_id == "kubernetes")
    assert k8s.status == FieldStatus.conflict and k8s.confidence == Confidence.low


def test_must_have_needs_verified_cue_quote():
    unverified = PostingExtraction(skills=[{"name": "SQL", "bucket": "must_have", "kind": "named",
                                            "quote": "made up sentence"}])
    draft = merge_draft(TEXT, {}, [], unverified, [])
    assert next(s for s in draft.skills if s.skill_id == "sql").bucket == "required"

    verified = PostingExtraction(skills=[{"name": "Python", "bucket": "must_have", "kind": "named",
                                          "quote": "Strong Python skills are required"}])
    draft = merge_draft(TEXT, {}, [_det_skill("python")], verified, [])
    assert next(s for s in draft.skills if s.skill_id == "python").bucket == "must_have"


def test_injection_flag_forces_review_on_everything():
    llm = PostingExtraction(skills=[{"name": "Python", "bucket": "required", "kind": "named",
                                     "quote": "Strong Python skills are required"}])
    draft = merge_draft(TEXT, {}, [_det_skill("python")], llm,
                        ["injection_suspected:phrase"])
    assert all(s.status != FieldStatus.auto for s in draft.skills)


def test_impossible_deadline_does_not_crash_extraction():
    r"""A syntactic-but-invalid det date (e.g. 2025-13-45 from \d{4}-\d{2}-\d{2}) must be flagged
    for review, never raise ValueError and 500 the whole posting extraction."""
    det = {"application_deadline": ExtractedField(
        value="2025-13-45", source_span=(0, 10), method=Method.regex,
        confidence=Confidence.high, status=FieldStatus.auto)}
    draft = merge_draft("Apply by 2025-13-45.", det, [], None, [])
    assert draft.application_deadline.status == FieldStatus.conflict
