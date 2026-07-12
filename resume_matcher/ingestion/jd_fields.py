"""P2/P3 — deterministic JD field extractors + anomaly scan (docs/JD_AUTOFILL.md §2).

Each extractor returns an `ExtractedField` with the char span it fired on, `method != llm`, and
(for exact regex hits) `confidence: high` — these are the fields the review UI may auto-accept.
The email/URL/phone patterns are the redaction chokepoint's own regexes INVERTED to capture
(a JD's contact/apply coordinates are wanted outputs; on a resume they'd be PII to strip).

Skills are scanned PER SECTION (`normalize_skills` over SKILL_SECTIONS only) and bucketed by the
section that produced them — the structural fix for the junk-detection classes. Also home of
`skill_ids_from_names` (promoted out of api/demo.py so it stops being a demo-module private).
"""
from __future__ import annotations

import re
import unicodedata

from ..inference.posting_schema import (
    EMPLOYMENT_TYPES,
    ExtractedField,
    FieldStatus,
    Method,
    SkillDraft,
    WORK_MODES,
)
from ..inference.redaction import EMAIL_RE, PHONE_RE, URL_RE
from ..inference.schema import Confidence
from ..matching.taxonomy import canonical_name, normalize_skills
from .jd_structure import SKILL_SECTIONS, Section, bullet_lines, section_text, sections_of
from .parser import infer_education_level, infer_years_experience

# ---- skill-name resolution (promoted from api/demo.py; same behavior, same tests) ---------------
_SLUG_RE = re.compile(r"[^\w+#.]+")


def skill_ids_from_names(names) -> list[str]:
    """LLM skill NAMES -> skill ids. A name containing a known taxonomy surface resolves to the
    canonical id (synonyms collapse); anything unknown gets a conservative slug, because the ranker
    and both extraction engines match skills against the resume TEXT, not the taxonomy."""
    out: list[str] = []
    for name in names if isinstance(names, list) else []:
        if not isinstance(name, str) or not name.strip():
            continue
        ids = normalize_skills(name)
        if not ids:
            slug = _SLUG_RE.sub("_", unicodedata.normalize("NFKC", name.strip()).lower()).strip("_")
            ids = [slug] if 2 <= len(slug) <= 40 else []
        for sid in ids:
            if sid not in out:
                out.append(sid)
    return out


# ---- pay ----------------------------------------------------------------------------------------
_PAY_RE = re.compile(
    r"(?P<cur>CAD|USD|C\$|\$)\s?(?P<min>\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    r"(?:\s*(?:-|–|—|to)\s*(?:CAD|USD|C\$|\$)?\s?(?P<max>\d{1,3}(?:,\d{3})*(?:\.\d+)?))?"
    r"\s*(?P<per>per\s+(?:hour|week|month|year|annum)|/\s*(?:hr|hour|wk|week|mo|month|yr|year)"
    r"|hourly|annually|per annum|an hour|a year)?",
    re.IGNORECASE,
)
_PERIOD_MAP = {
    "hr": "hour", "hour": "hour", "hourly": "hour", "an hour": "hour",
    "wk": "week", "week": "week",
    "mo": "month", "month": "month",
    "yr": "year", "year": "year", "annum": "year", "annually": "year", "a year": "year",
}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def extract_pay(text: str, sections: list[Section]) -> ExtractedField | None:
    """First pay-shaped match, preferring the pay_benefits section. Exact regex hit -> high."""
    ordered = sections_of(sections, "pay_benefits") + sections_of(
        sections, "header", "application", "other", "qualifications_required")
    for sec in ordered:
        m = _PAY_RE.search(text, sec.start, sec.end)
        if not m:
            continue
        lo = _num(m.group("min"))
        hi = _num(m.group("max")) if m.group("max") else None
        per_raw = (m.group("per") or "").lower()
        period = next((v for k, v in _PERIOD_MAP.items() if k in per_raw), None)
        if period is None:  # infer from magnitude: nobody pays $22/year or $80,000/hour
            period = "hour" if lo < 500 else "year"
        cur = (m.group("cur") or "").upper()
        currency = {"C$": "CAD", "$": "CAD", "CAD": "CAD", "USD": "USD"}.get(cur, "CAD")
        value = {"min": lo, "max": hi if hi is not None else lo, "currency": currency,
                 "period": period}
        return ExtractedField(value=value, source_span=(m.start(), m.end()), method=Method.regex,
                              confidence=Confidence.high, status=FieldStatus.auto)
    return None


# ---- dates (deadline vs start, disambiguated by cue words) --------------------------------------
_MONTHS = {m.lower()[:3]: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_DATE_RE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|(?P<mon>Jan\w*|Feb\w*|Mar\w*|Apr\w*|May|Jun\w*|Jul\w*|Aug\w*|Sep\w*|Oct\w*|Nov\w*|Dec\w*)"
    r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_DEADLINE_CUES = re.compile(r"apply (?:by|before)|deadline|applications? (?:close|due)"
                            r"|closing date|date limite", re.IGNORECASE)
_START_CUES = re.compile(r"start(?:s|ing)? (?:date|on)?|begins?|commenc|term (?:begins|starts)"
                         r"|date de d[ée]but", re.IGNORECASE)


def _iso(m: re.Match) -> str | None:
    if m.group("iso"):
        return m.group("iso")
    month = _MONTHS.get(m.group("mon")[:3].lower())
    if not month:
        return None
    return f"{m.group('year')}-{month:02d}-{int(m.group('day')):02d}"


def extract_dates(text: str) -> dict[str, ExtractedField]:
    """{application_deadline?, start_date?} — a date only counts when a cue word sits within the
    60 chars before it (an undated JD stays undated; no guessing)."""
    out: dict[str, ExtractedField] = {}
    for m in _DATE_RE.finditer(text):
        iso = _iso(m)
        if not iso:
            continue
        lead = text[max(0, m.start() - 60):m.start()]
        field = ExtractedField(value=iso, source_span=(m.start(), m.end()), method=Method.regex,
                               confidence=Confidence.high, status=FieldStatus.auto)
        if _DEADLINE_CUES.search(lead) and "application_deadline" not in out:
            out["application_deadline"] = field
        elif _START_CUES.search(lead) and "start_date" not in out:
            out["start_date"] = field
    return out


# ---- contact / application coordinates ----------------------------------------------------------
_APPLY_URL_CUES = re.compile(r"apply|careers?|jobs?\.|greenhouse\.io|lever\.co|workday|smartrecruiters"
                             r"|bamboohr|postuler", re.IGNORECASE)


def extract_contacts(text: str) -> dict[str, ExtractedField]:
    """Emails/URLs/phone as wanted outputs. URL classified apply-vs-info by anchor words."""
    out: dict[str, ExtractedField] = {}
    m = EMAIL_RE.search(text)
    if m:
        out["application_email"] = ExtractedField(
            value=m.group(0), source_span=(m.start(), m.end()), method=Method.regex,
            confidence=Confidence.high, status=FieldStatus.auto)
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,);")
        line_start = text.rfind("\n", 0, m.start()) + 1
        context = text[line_start:m.end()]
        key = "application_url" if _APPLY_URL_CUES.search(context) else "employer_website"
        if key not in out:
            out[key] = ExtractedField(value=url, source_span=(m.start(), m.end()),
                                      method=Method.regex, confidence=Confidence.high,
                                      status=FieldStatus.auto)
    m = PHONE_RE.search(text)
    if m:
        out["contact_phone"] = ExtractedField(value=m.group(0), source_span=(m.start(), m.end()),
                                              method=Method.regex, confidence=Confidence.high,
                                              status=FieldStatus.auto)
    return out


# ---- employment type / work mode ----------------------------------------------------------------
_TYPE_TABLE: list[tuple[str, str]] = [
    ("co-op", "co_op"), ("co op", "co_op"), ("coop", "co_op"),
    ("internship", "internship"), ("intern", "internship"),
    ("new grad", "new_grad"), ("new graduate", "new_grad"),
    ("work-study", "work_study"), ("work study", "work_study"),
    ("full-time", "full_time"), ("full time", "full_time"),
    ("part-time", "part_time"), ("part time", "part_time"),
    ("contract", "contract"),
]
_MODE_TABLE: list[tuple[str, str]] = [
    ("fully remote", "remote"), ("remote", "remote"),
    ("hybrid", "hybrid"),
    ("on-site", "onsite"), ("onsite", "onsite"), ("on site", "onsite"),
    ("in-office", "onsite"), ("in office", "onsite"),
]


def _keyword_field(scope: str, offset: int, table: list[tuple[str, str]],
                   allowed: tuple[str, ...]) -> ExtractedField | None:
    low = scope.lower()
    for surface, value in table:
        idx = low.find(surface)
        if idx >= 0 and value in allowed:
            return ExtractedField(value=value, source_span=(offset + idx, offset + idx + len(surface)),
                                  method=Method.heuristic, confidence=Confidence.medium,
                                  status=FieldStatus.needs_review)
    return None


def extract_type_and_mode(text: str, sections: list[Section]) -> dict[str, ExtractedField]:
    """Scoped to the header (title area) first, then the whole text — 'hybrid' in a benefits blurb
    shouldn't beat 'Remote' in the title, and vice versa."""
    out: dict[str, ExtractedField] = {}
    scopes = [(s.start, text[s.start:s.end]) for s in sections_of(sections, "header")]
    scopes.append((0, text))
    for offset, scope in scopes:
        if "employment_type" not in out:
            f = _keyword_field(scope, offset, _TYPE_TABLE, EMPLOYMENT_TYPES)
            if f:
                out["employment_type"] = f
        if "work_mode" not in out:
            f = _keyword_field(scope, offset, _MODE_TABLE, WORK_MODES)
            if f:
                out["work_mode"] = f
    return out


# ---- education / years (scoped to qualifications so about_company can't fire) -------------------
def extract_min_requirements(text: str, sections: list[Section]) -> dict[str, ExtractedField]:
    quals = section_text(text, sections, "qualifications_required", "qualifications_preferred")
    scope = quals or section_text(text, sections, "header")  # sectionless JDs: header only, never
    out: dict[str, ExtractedField] = {}                      # the whole document
    edu = infer_education_level(scope)
    if edu:
        out["min_education"] = ExtractedField(value=edu, method=Method.heuristic,
                                              confidence=Confidence.medium,
                                              status=FieldStatus.needs_review)
    years = infer_years_experience(scope)
    if years:
        out["min_years"] = ExtractedField(value=years, method=Method.heuristic,
                                          confidence=Confidence.medium,
                                          status=FieldStatus.needs_review)
    return out


# ---- bullets ------------------------------------------------------------------------------------
def extract_bullets(text: str, sections: list[Section]) -> dict[str, ExtractedField]:
    out: dict[str, ExtractedField] = {}
    spec = [("responsibilities", ("responsibilities",), 15),
            ("qualifications_required", ("qualifications_required",), 15),
            ("qualifications_preferred", ("qualifications_preferred",), 10)]
    for field, kinds, limit in spec:
        lines: list[str] = []
        first_span: tuple[int, int] | None = None
        for sec in sections_of(sections, *kinds):
            for line, span in bullet_lines(text, sec, limit - len(lines)):
                lines.append(line)
                first_span = first_span or span
        if lines:
            out[field] = ExtractedField(value=lines, source_span=first_span,
                                        method=Method.heuristic, confidence=Confidence.medium,
                                        status=FieldStatus.needs_review)
    return out


# ---- skills: SECTION-SCOPED taxonomy scan (the junk fix) -----------------------------------------
_SECTION_BUCKET = {
    "header": ("required", "named"),
    "qualifications_required": ("required", "named"),
    "qualifications_preferred": ("preferred", "named"),
    "responsibilities": ("preferred", "demonstrated"),  # implied by duties until the LLM promotes
}
_BUCKET_RANK = {"must_have": 0, "required": 1, "preferred": 2}


def extract_skills(text: str, sections: list[Section]) -> list[SkillDraft]:
    """`normalize_skills` per skill-bearing section; a skill found in several keeps the strongest
    bucket. about_company/pay_benefits/eeo are never scanned — junk dies by construction."""
    drafts: dict[str, SkillDraft] = {}
    for sec in sections_of(sections, *SKILL_SECTIONS):
        bucket, kind = _SECTION_BUCKET[sec.kind]
        for sid in normalize_skills(text[sec.start:sec.end]):
            existing = drafts.get(sid)
            if existing is None:
                drafts[sid] = SkillDraft(
                    skill_id=sid, name=canonical_name(sid), bucket=bucket, kind=kind,
                    method=Method.taxonomy, confidence=Confidence.medium,
                    status=FieldStatus.needs_review, source_span=(sec.start, sec.end))
            elif _BUCKET_RANK[bucket] < _BUCKET_RANK[existing.bucket]:
                existing.bucket, existing.kind = bucket, kind
                existing.source_span = (sec.start, sec.end)
    return list(drafts.values())


# ---- title heuristic -----------------------------------------------------------------------------
def extract_title(text: str, sections: list[Section]) -> ExtractedField | None:
    """First short non-bullet header line. Low confidence — the LLM usually wins the merge."""
    header = sections_of(sections, "header")
    if not header:
        return None
    sec = header[0]
    offset = sec.start
    for line in text[sec.start:sec.end].splitlines(keepends=True):
        stripped = line.strip()
        if stripped and len(stripped) <= 90 and stripped[0] not in "-•*·":
            start = offset + line.index(stripped[0])
            return ExtractedField(value=stripped, source_span=(start, start + len(stripped)),
                                  method=Method.heuristic, confidence=Confidence.low,
                                  status=FieldStatus.needs_review)
        offset += len(line)
    return None


# ---- P3: anomaly scan ----------------------------------------------------------------------------
_EN_STOP = {"the", "and", "to", "of", "a", "in", "for", "with", "you", "we", "our", "is", "are"}
_FR_STOP = {"le", "la", "les", "et", "de", "des", "un", "une", "pour", "avec", "vous", "nous",
            "est", "sont", "dans"}
_TITLE_WORDS = re.compile(
    r"^(?!.*[.:,;]$)\s*[A-Z][\w&/()' -]{4,70}\b(intern(ship)?|analyst|developer|engineer"
    r"|coordinator|assistant|manager|designer|scientist|specialist|associate)\b.{0,20}$",
    re.IGNORECASE | re.MULTILINE)


def anomaly_flags(text: str) -> list[str]:
    """P3 flags: injection markers (never blocks — forces review + banners), language, multi-role.
    Deterministic and un-injectable by construction."""
    from ..antigaming.injection import scan_injection

    flags = [f"injection_suspected:{f}" for f in scan_injection(text)]
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿ']+", text.lower())
    if len(words) >= 40:
        en = sum(w in _EN_STOP for w in words)
        fr = sum(w in _FR_STOP for w in words)
        if fr > en:
            flags.append("non_english:fr")
        elif en / max(1, len(words)) < 0.02:
            flags.append("non_english:unknown")
    title_lines = {m.group(0).strip() for m in _TITLE_WORDS.finditer(text)}
    if len(title_lines) >= 3:  # conservative: title + one repeat is normal; 3+ smells multi-role
        flags.append("multi_role_suspected")
    if len(text.strip()) < 120:
        flags.append("low_text")
    return flags
