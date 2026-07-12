"""P5 — merge, verify, confidence: THE decision authority of the JD-autofill pipeline
(docs/JD_AUTOFILL.md §2). The direct analogue of ranker.score()'s evidence verification, applied
to posting fields: the LLM proposed values with quotes; deterministic code verifies every quote is
a verbatim substring of the JD, merges against the P2 extractors, clamps types/ranges, and computes
per-field confidence + review status. The LLM holds no authority over what lands in the form.

Merge policy (deterministic table):
  * P2 and LLM agree ................................. high, auto
  * P2 exact-regex hit only .......................... keeps P2's own high/auto
  * LLM only, quote verified ......................... medium, needs_review
  * LLM only, quote unverified ....................... low, conflict (never silently in)
  * P2 vs LLM disagree ............................... P2 pre-fills, LLM shown as candidate; conflict
  * Nothing .......................................... missing
Fields on the always-confirm list (title, employment_type, pay, deadline, application) never
auto-accept regardless of confidence — Ontario pay-transparency liability lives there.
"""
from __future__ import annotations

import datetime as _dt
import re

from ..inference.posting_schema import (
    EDU_LEVELS,
    EMPLOYMENT_TYPES,
    PAY_PERIODS,
    SKILL_BUCKETS,
    WORK_MODES,
    ExtractedField,
    FieldStatus,
    JobPosting,
    Method,
    PostingExtraction,
    SkillDraft,
)
from ..inference.schema import Confidence
from ..matching.taxonomy import canonical_name
from .jd_fields import skill_ids_from_names

_MIN_QUOTE_ALNUM = 3  # same floor as the ranker: shorter "quotes" match anything
_WS_RE = re.compile(r"\s+")

# Fields that must be human-confirmed even at high confidence (policy table, JD_AUTOFILL §4).
ALWAYS_CONFIRM = {"title", "employment_type", "pay", "application_deadline", "application"}

_MUST_CUES = re.compile(r"must[\s-]have|must be|required|non[\s-]negotiable|mandatory"
                        r"|licen[cs]e|certification", re.IGNORECASE)

_TYPE_SYNONYMS = {"permanent": "full_time", "fulltime": "full_time", "full-time": "full_time",
                  "parttime": "part_time", "part-time": "part_time", "coop": "co_op",
                  "co-op": "co_op", "intern": "internship", "temporary": "contract"}
_EDU_SYNONYMS = {"high school": "highschool", "doctorate": "phd", "doctoral": "phd",
                 "certificate": "diploma", "college diploma": "diploma",
                 "undergraduate": "bachelor", "graduate": "master"}
_CURRENCIES = {"CAD", "USD", "EUR", "GBP"}
_PAY_SANITY = {"hour": (10, 200), "week": (400, 8000), "month": (1500, 40000),
               "year": (20000, 500000), "stipend": (500, 100000), "unpaid": (0, 0)}


# ---- verbatim quote verification (shared spirit with matching/ranker.py _verify) -----------------
def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, lowercased copy of `text` plus a norm-index -> original-index map, so a
    match in normalized space can be projected back to real char offsets (the provenance currency)."""
    chars: list[str] = []
    idx_map: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            chars.append(" ")
            idx_map.append(i)
            prev_space = True
        else:
            chars.append(ch.lower())
            idx_map.append(i)
            prev_space = False
    if chars and chars[-1] == " ":
        chars.pop()
        idx_map.pop()
    return "".join(chars), idx_map


def verify_span(text: str, quote: str | None) -> tuple[int, int] | None:
    """Char span of `quote` in `text` when it is a MEANINGFUL verbatim substring (whitespace-
    normalized, case-insensitive), else None. Degenerate quotes (< 3 alphanumerics) are rejected —
    they'd match anything and let an injected posting fabricate 'verified' fields."""
    if not quote:
        return None
    q = quote.strip()
    if sum(ch.isalnum() for ch in q) < _MIN_QUOTE_ALNUM:
        return None
    exact = text.find(q)
    if exact >= 0:
        return (exact, exact + len(q))
    norm_text, idx_map = _normalize_with_map(text)
    norm_q, _ = _normalize_with_map(q)
    pos = norm_text.find(norm_q)
    if pos < 0 or not norm_q:
        return None
    return (idx_map[pos], idx_map[pos + len(norm_q) - 1] + 1)


# ---- scalar validators / clamps ------------------------------------------------------------------
def _clean_enum(value, allowed: tuple[str, ...], synonyms: dict[str, str] | None = None):
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    v = (synonyms or {}).get(v, v)
    return v if v in allowed else None


def _clean_date(value):
    if not isinstance(value, str):
        return None
    try:
        return _dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _clean_years(value):
    return float(value) if isinstance(value, (int, float)) and 0 <= float(value) <= 30 else None


def _clean_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    return v if v.startswith(("https://", "http://")) else None


def _clean_pay(pay) -> dict | None:
    """Normalize an LLM pay object; returns {min,max,currency,period} or None."""
    if pay is None:
        return None
    lo, hi = pay.min, pay.max
    if lo is None and hi is None:
        return None
    lo = float(lo) if lo is not None else float(hi)
    hi = float(hi) if hi is not None else lo
    if lo > hi:
        lo, hi = hi, lo
    currency = (pay.currency or "CAD").upper()
    if currency not in _CURRENCIES:
        currency = "CAD"
    period = _clean_enum(pay.period, PAY_PERIODS) or ("hour" if lo < 500 else "year")
    return {"min": lo, "max": hi, "currency": currency, "period": period}


def _pay_sane(value: dict) -> bool:
    lo, hi = _PAY_SANITY.get(value.get("period", ""), (0, 10 ** 9))
    return lo <= value["min"] <= hi and lo <= value["max"] <= hi


# ---- field merge ---------------------------------------------------------------------------------
def _values_agree(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return _WS_RE.sub(" ", a).strip().lower() == _WS_RE.sub(" ", b).strip().lower()
    return a == b


def _merge_scalar(
    text: str,
    det: ExtractedField | None,
    llm_value,
    llm_quote: str | None,
) -> ExtractedField:
    """The merge-policy table for one scalar field (llm_value is already validated/clamped)."""
    span = verify_span(text, llm_quote) if llm_value is not None else None
    if det is not None and llm_value is not None:
        if _values_agree(det.value, llm_value):
            return ExtractedField(value=det.value, source_span=det.source_span or span,
                                  method=Method.merged, confidence=Confidence.high,
                                  status=FieldStatus.auto)
        return ExtractedField(value=det.value, source_span=det.source_span, method=det.method,
                              confidence=Confidence.low, status=FieldStatus.conflict,
                              candidates=[{"value": llm_value, "method": "llm",
                                           "source_span": list(span) if span else None}])
    if det is not None:
        return det
    if llm_value is not None:
        if span:
            return ExtractedField(value=llm_value, source_span=span, method=Method.llm,
                                  confidence=Confidence.medium, status=FieldStatus.needs_review)
        return ExtractedField(value=llm_value, method=Method.llm, confidence=Confidence.low,
                              status=FieldStatus.conflict)
    return ExtractedField()


def _quoted(qs) -> tuple[object, str | None]:
    """(value, quote) from a QuotedStr-ish or None."""
    if qs is None:
        return None, None
    return (qs.value or None), qs.quote


_BUCKET_RANK = {"must_have": 0, "required": 1, "preferred": 2}
_BUCKET_CAPS = {"must_have": 4, "required": 12, "preferred": 10}


def _merge_skills(text: str, det_skills: list[SkillDraft],
                  llm_skills) -> list[SkillDraft]:
    """Dual-detector skill merge (JD_AUTOFILL §3): agreement -> high/auto; LLM-only verified ->
    medium/needs_review; LLM-only unverified -> low/conflict (greyed chip — never silently in);
    must_have needs a verified quote with an explicit deal-breaker cue."""
    out: dict[str, SkillDraft] = {s.skill_id: s.model_copy() for s in det_skills}
    for ls in llm_skills or []:
        ids = skill_ids_from_names([ls.name])
        if not ids:
            continue
        sid = ids[0]
        span = verify_span(text, ls.quote)
        bucket = ls.bucket if ls.bucket in SKILL_BUCKETS else "required"
        kind = ls.kind if ls.kind in ("named", "demonstrated") else "named"
        if bucket == "must_have" and not (span and _MUST_CUES.search(text[span[0]:span[1]])):
            bucket = "required"  # deal-breaker status needs a verified cue-bearing quote
        existing = out.get(sid)
        if existing is not None:
            existing.method = Method.merged
            existing.confidence = Confidence.high
            existing.status = FieldStatus.auto
            existing.source_span = span or existing.source_span
            if _BUCKET_RANK[bucket] < _BUCKET_RANK[existing.bucket]:
                existing.bucket = bucket
            if kind == "named":
                existing.kind = "named"
        else:
            out[sid] = SkillDraft(
                skill_id=sid, name=(ls.name or "").strip() or canonical_name(sid),
                bucket=bucket, kind=kind, method=Method.llm,
                confidence=Confidence.medium if span else Confidence.low,
                status=FieldStatus.needs_review if span else FieldStatus.conflict,
                source_span=span)
    # per-bucket caps, best-evidenced first
    ranked = sorted(out.values(), key=lambda s: (
        s.status != FieldStatus.auto,
        [Confidence.high, Confidence.medium, Confidence.low].index(s.confidence)))
    kept: list[SkillDraft] = []
    counts = {b: 0 for b in SKILL_BUCKETS}
    for s in ranked:
        if counts[s.bucket] < _BUCKET_CAPS[s.bucket]:
            counts[s.bucket] += 1
            kept.append(s)
    return kept


def merge_draft(
    text: str,
    det_fields: dict[str, ExtractedField],
    det_skills: list[SkillDraft],
    llm: PostingExtraction | None,
    flags: list[str],
) -> JobPosting:
    """Assemble the reviewable JobPosting draft from the deterministic pass + (optional) LLM pass."""
    L = llm or PostingExtraction()

    title_v, title_q = _quoted(L.title)
    employer_v, employer_q = _quoted(L.employer_name)
    deadline_v, deadline_q = _quoted(L.application_deadline)
    start_v, start_q = _quoted(L.start_date)

    pay_llm = _clean_pay(L.pay)
    pay_field = _merge_scalar(text, det_fields.get("pay"), pay_llm,
                              L.pay.quote if L.pay else None)
    if pay_field.value and not _pay_sane(pay_field.value):
        pay_field.confidence = Confidence.low
        pay_field.status = FieldStatus.conflict  # implausible band — force an explicit decision

    deadline_field = _merge_scalar(text, det_fields.get("application_deadline"),
                                   _clean_date(deadline_v), deadline_q)
    if deadline_field.value:
        # The deterministic regex \d{4}-\d{2}-\d{2} can capture a syntactically-valid but impossible
        # date (e.g. 2025-13-45); re-validate before comparing so a bad date never raises ValueError
        # and 500s the whole extraction — flag it for human correction instead.
        parsed = _clean_date(deadline_field.value)
        if parsed is None:
            deadline_field.confidence = Confidence.low
            deadline_field.status = FieldStatus.conflict
        elif _dt.date.fromisoformat(parsed) < _dt.date.today():
            deadline_field.confidence = Confidence.low
            deadline_field.status = FieldStatus.needs_review  # stale/past deadline: confirm, don't drop

    # application: method + url + email folded into one envelope
    det_url = det_fields.get("application_url")
    det_email = det_fields.get("application_email")
    url_field = _merge_scalar(text, det_url, _clean_url(L.application_url), None)
    email_field = _merge_scalar(text, det_email, L.application_email, None)
    method = _clean_enum(L.application_method, ("platform", "external_url", "email"))
    if method is None:
        method = "external_url" if url_field.value else ("email" if email_field.value else None)
    application = ExtractedField(
        value={"method": method, "url": url_field.value, "email": email_field.value},
        source_span=url_field.source_span or email_field.source_span,
        method=url_field.method or email_field.method,
        confidence=min((f.confidence for f in (url_field, email_field) if f.confidence),
                       key=lambda c: [Confidence.high, Confidence.medium, Confidence.low].index(c),
                       default=None),
        status=FieldStatus.missing if (not url_field.value and not email_field.value)
        else FieldStatus.needs_review,
    )

    locations_llm = [v for v, _ in (_quoted(loc) for loc in L.locations) if v] or None
    loc_quote = L.locations[0].quote if L.locations else None

    draft = JobPosting(
        title=_merge_scalar(text, det_fields.get("title"), title_v, title_q),
        employer_name=_merge_scalar(text, det_fields.get("employer_name"), employer_v, employer_q),
        employer_website=_merge_scalar(text, det_fields.get("employer_website"),
                                       _clean_url(L.employer_website), None),
        locations=_merge_scalar(text, det_fields.get("locations"), locations_llm, loc_quote),
        work_mode=_merge_scalar(text, det_fields.get("work_mode"),
                                _clean_enum(L.work_mode, WORK_MODES), None),
        employment_type=_merge_scalar(text, det_fields.get("employment_type"),
                                      _clean_enum(L.employment_type, EMPLOYMENT_TYPES,
                                                  _TYPE_SYNONYMS), None),
        pay=pay_field,
        application_deadline=deadline_field,
        start_date=_merge_scalar(text, det_fields.get("start_date"), _clean_date(start_v), start_q),
        description=text,
        responsibilities=_merge_scalar(text, det_fields.get("responsibilities"),
                                       [r for r in L.responsibilities if isinstance(r, str)][:15]
                                       or None, None),
        qualifications_required=_merge_scalar(
            text, det_fields.get("qualifications_required"),
            [q for q in L.qualifications_required if isinstance(q, str)][:15] or None, None),
        qualifications_preferred=_merge_scalar(
            text, det_fields.get("qualifications_preferred"),
            [q for q in L.qualifications_preferred if isinstance(q, str)][:10] or None, None),
        skills=_merge_skills(text, det_skills, L.skills),
        min_education=_merge_scalar(text, det_fields.get("min_education"),
                                    _clean_enum(L.min_education, EDU_LEVELS, _EDU_SYNONYMS), None),
        min_years=_merge_scalar(text, det_fields.get("min_years"), _clean_years(L.min_years), None),
        work_authorization=ExtractedField(
            value={"statement": L.work_authorization_statement,
                   "sponsorship_available": L.sponsorship_available},
            method=Method.llm, confidence=Confidence.low,
            status=FieldStatus.conflict if L.work_authorization_statement else FieldStatus.missing,
            source_span=verify_span(text, L.work_authorization_statement)),
        application=application,
        contact=ExtractedField(
            value={"phone": det_fields["contact_phone"].value} if "contact_phone" in det_fields
            else None,
            source_span=det_fields["contact_phone"].source_span
            if "contact_phone" in det_fields else None,
            method=Method.regex if "contact_phone" in det_fields else None,
            status=FieldStatus.needs_review if "contact_phone" in det_fields
            else FieldStatus.missing),
    )

    # Injection-flagged documents: nothing auto-accepts (P3 forces review; banner in the UI).
    if any(f.startswith("injection_suspected") for f in flags):
        for name in ("title", "employer_name", "pay", "application_deadline", "start_date",
                     "work_mode", "employment_type", "locations"):
            fld: ExtractedField = getattr(draft, name)
            if fld.status == FieldStatus.auto:
                fld.status = FieldStatus.needs_review
        for s in draft.skills:
            if s.status == FieldStatus.auto:
                s.status = FieldStatus.needs_review

    # Always-confirm fields never auto-accept (pay transparency et al).
    for name in ALWAYS_CONFIRM - {"application"}:
        fld = getattr(draft, name)
        if fld.status == FieldStatus.auto:
            fld.status = FieldStatus.needs_review

    return draft
