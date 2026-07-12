"""P1 sectionizer (ingestion/jd_structure.py): typed blocks, offsets, bullets, skill scoping."""
from __future__ import annotations

from pathlib import Path

from resume_matcher.ingestion.jd_structure import (
    SKILL_SECTIONS,
    bullet_lines,
    section_text,
    sections_of,
    sectionize,
)

FIXTURES = Path(__file__).parent / "fixtures" / "jds"
CLEAN = (FIXTURES / "clean_en.txt").read_text(encoding="utf-8")


def test_sections_cover_text_contiguously():
    sections = sectionize(CLEAN)
    assert sections[0].start == 0
    for prev, cur in zip(sections, sections[1:]):
        assert prev.end == cur.start
    assert sections[-1].end == len(CLEAN)


def test_kinds_detected():
    kinds = [s.kind for s in sectionize(CLEAN)]
    assert kinds[0] == "header"
    for expected in ("about_company", "responsibilities", "qualifications_required",
                     "qualifications_preferred", "pay_benefits", "application",
                     "eeo_boilerplate"):
        assert expected in kinds, f"missing {expected} in {kinds}"


def test_offsets_are_true_spans():
    sections = sectionize(CLEAN)
    resp = sections_of(sections, "responsibilities")[0]
    assert "What You'll Do" in CLEAN[resp.start:resp.end]
    assert "Pull raw order data" in CLEAN[resp.start:resp.end]
    assert "About Us" not in CLEAN[resp.start:resp.end]


def test_bullets_extracted_with_absolute_spans():
    sections = sectionize(CLEAN)
    req = sections_of(sections, "qualifications_required")[0]
    bullets = bullet_lines(CLEAN, req)
    assert any("SQL" in b for b, _ in bullets)
    text, (start, end) = bullets[0]
    assert CLEAN[start:end] == text


def test_bulleted_nice_to_have_line_is_not_a_heading():
    text = "Requirements\n- Python\n- Nice to have: Docker familiarity\n- SQL\n"
    sections = sectionize(text)
    assert [s.kind for s in sections].count("qualifications_preferred") == 0


def test_headingless_posting_is_all_header():
    sections = sectionize("We need a Python developer with SQL. Email hr@x.com.")
    assert [s.kind for s in sections] == ["header"]


def test_skill_scoped_sections_exclude_boilerplate():
    assert "about_company" not in SKILL_SECTIONS
    assert "pay_benefits" not in SKILL_SECTIONS
    assert "eeo_boilerplate" not in SKILL_SECTIONS
    # and the scoped text helper reflects that
    sections = sectionize(CLEAN)
    scoped = section_text(CLEAN, sections, *SKILL_SECTIONS)
    assert "Kafka" not in scoped            # named only in about_company
    assert "Python" in scoped
