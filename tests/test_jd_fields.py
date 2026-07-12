"""P2/P3 deterministic extractors (ingestion/jd_fields.py): pay, dates, contacts, type/mode,
scoped education/years, section-scoped skills (the junk fix), anomaly flags."""
from __future__ import annotations

from pathlib import Path

from resume_matcher.ingestion.jd_fields import (
    anomaly_flags,
    extract_bullets,
    extract_contacts,
    extract_dates,
    extract_min_requirements,
    extract_pay,
    extract_skills,
    extract_title,
    extract_type_and_mode,
    skill_ids_from_names,
)
from resume_matcher.ingestion.jd_structure import sectionize
from resume_matcher.inference.schema import Confidence

FIXTURES = Path(__file__).parent / "fixtures" / "jds"
CLEAN = (FIXTURES / "clean_en.txt").read_text(encoding="utf-8")
INJECTED = (FIXTURES / "injection.txt").read_text(encoding="utf-8")
SECTIONS = sectionize(CLEAN)


def test_pay_range_parsed_high_confidence():
    pay = extract_pay(CLEAN, SECTIONS)
    assert pay.value == {"min": 24.0, "max": 28.0, "currency": "CAD", "period": "hour"}
    assert pay.confidence == Confidence.high
    assert CLEAN[pay.source_span[0]:pay.source_span[1]].startswith("$24")


def test_annual_salary_period_inferred_from_magnitude():
    text = "Compensation\nWe pay $65,000 - $80,000 for this role.\n"
    pay = extract_pay(text, sectionize(text))
    assert pay.value["period"] == "year" and pay.value["min"] == 65000.0


def test_deadline_vs_start_date_disambiguated_by_cues():
    dates = extract_dates(CLEAN)
    assert dates["application_deadline"].value == "2027-03-15"
    assert dates["start_date"].value == "2027-05-03"


def test_undated_text_yields_no_dates():
    assert extract_dates("The company was founded on June 1, 2010.") == {}


def test_contacts_classified():
    contacts = extract_contacts(CLEAN)
    assert contacts["application_email"].value == "recruiting@northwind.example.com"
    assert contacts["application_url"].value == "https://jobs.example.com/apply/da-intern"
    assert contacts["employer_website"].value == "https://northwind.example.com"


def test_type_and_mode_from_header():
    fields = extract_type_and_mode(CLEAN, SECTIONS)
    assert fields["employment_type"].value == "internship"
    assert fields["work_mode"].value == "hybrid"


def test_min_requirements_scoped_to_qualifications():
    reqs = extract_min_requirements(CLEAN, SECTIONS)
    assert reqs["min_education"].value == "bachelor"
    # "2+ years" from Requirements wins; "founded 12 years ago" in About Us can never fire
    assert reqs["min_years"].value == 2.0


def test_bullets_land_in_the_right_fields():
    bullets = extract_bullets(CLEAN, SECTIONS)
    assert any("dashboards" in line for line in bullets["responsibilities"].value)
    assert any("SQL" in line for line in bullets["qualifications_required"].value)
    assert any("Tableau" in line for line in bullets["qualifications_preferred"].value)


def test_skills_section_scoped_and_bucketed():
    skills = {s.skill_id: s for s in extract_skills(CLEAN, SECTIONS)}
    assert "kafka" not in skills                 # about_company only -> junk dies by construction
    assert "snowflake" not in skills
    assert skills["python"].bucket == "required"
    assert skills["sql"].bucket == "required"
    assert skills["tableau"].bucket == "preferred"
    # a skill seen only in responsibilities is a 'demonstrated' preferred candidate
    resp_only = [s for s in skills.values() if s.kind == "demonstrated"]
    assert all(s.bucket == "preferred" for s in resp_only)


def test_required_section_beats_responsibilities_bucket():
    text = ("Role\n\nWhat You'll Do\n- Write Python pipelines\n\n"
            "Requirements\n- Python expertise\n")
    skills = {s.skill_id: s for s in extract_skills(text, sectionize(text))}
    assert skills["python"].bucket == "required" and skills["python"].kind == "named"


def test_title_heuristic_is_low_confidence():
    title = extract_title(CLEAN, SECTIONS)
    assert "Data Analyst Intern" in title.value
    assert title.confidence == Confidence.low


def test_anomaly_flags_fire_on_injection():
    flags = anomaly_flags(INJECTED)
    assert any(f.startswith("injection_suspected") for f in flags)
    assert not any(f.startswith("injection_suspected") for f in anomaly_flags(CLEAN))


def test_skill_ids_from_names_matches_promoted_behavior():
    ids = skill_ids_from_names(["Python", "Zxqvbn Frobnication", "Python", "", 42])
    assert ids[:1] == ["python"] and "zxqvbn_frobnication" in ids and len(ids) == 2
