"""P1 — deterministic JD sectionizer (docs/JD_AUTOFILL.md §2).

Splits a posting into typed blocks with CHARACTER OFFSETS into the canonical text — the provenance
currency of the whole autofill pipeline (the review UI highlights `source_span`s; jd_merge verifies
LLM quotes against the same text). Regex-over-headings + bullet runs; cheap, testable, no ML.

Section scoping is also the structural fix for junk skill detection: only header/responsibilities/
qualifications sections may contribute skills (about_company / pay_benefits / eeo_boilerplate are
never scanned), so the historical URL-and-benefits-blurb junk classes die by construction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SECTION_KINDS = (
    "header", "about_company", "responsibilities", "qualifications_required",
    "qualifications_preferred", "pay_benefits", "application", "eeo_boilerplate", "other",
)

# Sections that may contribute SKILLS (docs/JD_AUTOFILL.md §3.1).
SKILL_SECTIONS = ("header", "responsibilities", "qualifications_required",
                  "qualifications_preferred")

# Heading surface -> section kind. English + the realistic-Canadian French set. Order matters:
# 'preferred' cues must be tested before the generic qualifications patterns.
_HEADING_RULES: list[tuple[str, re.Pattern]] = [
    ("qualifications_preferred", re.compile(
        r"nice[\s-]to[\s-]have|preferred qualification|preferred skills|bonus (points|skills|if)"
        r"|assets?\b|good to have|great to have|atouts", re.IGNORECASE)),
    ("qualifications_required", re.compile(
        r"requirements?\b|qualifications?\b|must[\s-]have|what (you.ll|we) need|who you are"
        r"|skills? (&|and) experience|required skills|minimum qualification|what we.re looking for"
        r"|about you|exigences|qualifications requises|profil recherch", re.IGNORECASE)),
    ("responsibilities", re.compile(
        r"responsibilit|duties|what you.ll (do|be doing)|what you will (do|be doing)"
        r"|the role\b|your role|role overview|day[\s-]to[\s-]day|in this role|key accountabilit"
        r"|vos responsabilit|fonctions", re.IGNORECASE)),
    ("pay_benefits", re.compile(
        r"compensation|salary|pay (range|rate)|benefits|perks|what we offer|why (join|work)"
        r"|r[ée]mun[ée]ration|avantages", re.IGNORECASE)),
    ("application", re.compile(
        r"how to apply|to apply\b|apply now|application (process|instructions|deadline)"
        r"|next steps|comment postuler", re.IGNORECASE)),
    ("about_company", re.compile(
        r"about (us|the (company|team|organi[sz]ation)|" + r"\w+ inc)|who we are|our (story|mission|company|team)"
        r"|company overview|[àa] propos", re.IGNORECASE)),
    ("eeo_boilerplate", re.compile(
        r"equal opportunity|eeo\b|diversity,? equity|accommodations?\b|accessibility"
        r"|[ée]quit[ée]|land acknowledg", re.IGNORECASE)),
]

_MAX_HEADING_LEN = 90


@dataclass
class Section:
    kind: str
    start: int  # char offset into the canonical text (inclusive)
    end: int    # exclusive
    heading: str = ""


def _heading_kind(line: str) -> str | None:
    """A short line matching a heading rule starts a new section. Bulleted lines never do —
    'Nice to have: familiarity with Docker' inside a list is content, not a heading."""
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_LEN:
        return None
    if stripped[0] in "-•*·–—":
        return None
    for kind, pattern in _HEADING_RULES:
        if pattern.search(stripped):
            return kind
    return None


def sectionize(text: str) -> list[Section]:
    """Split `text` into contiguous typed sections. Always covers [0, len(text)); the stretch
    before the first heading is the `header`."""
    sections: list[Section] = []
    current_kind, current_start, current_heading = "header", 0, ""
    offset = 0
    for line in text.splitlines(keepends=True):
        kind = _heading_kind(line)
        if kind is not None and offset > 0:
            sections.append(Section(current_kind, current_start, offset, current_heading))
            current_kind, current_start, current_heading = kind, offset, line.strip()
        elif kind is not None:  # heading on the very first line
            current_kind, current_heading = kind, line.strip()
        offset += len(line)
    sections.append(Section(current_kind, current_start, max(offset, current_start), current_heading))
    return [s for s in sections if s.end > s.start]


def sections_of(sections: list[Section], *kinds: str) -> list[Section]:
    return [s for s in sections if s.kind in kinds]


def section_text(text: str, sections: list[Section], *kinds: str) -> str:
    """Concatenated text of all sections of the given kinds (for scoped scans)."""
    return "\n".join(text[s.start:s.end] for s in sections_of(sections, *kinds))


_BULLET_RE = re.compile(r"^\s*(?:[-•*·–—]|\d{1,2}[.)])\s+(.*\S)", re.MULTILINE)


def bullet_lines(text: str, section: Section, limit: int = 15) -> list[tuple[str, tuple[int, int]]]:
    """Bulleted lines inside a section as (text, absolute char span), capped."""
    out: list[tuple[str, tuple[int, int]]] = []
    for m in _BULLET_RE.finditer(text, section.start, section.end):
        out.append((m.group(1).strip(), (m.start(1), m.end(1))))
        if len(out) >= limit:
            break
    return out
