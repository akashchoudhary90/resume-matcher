"""Resume parsing -> structured CandidateProfile.

Redaction happens HERE, at ingestion, so PII never propagates downstream (boundary #3). Skills are
normalized to canonical IDs via the taxonomy. PDF parsing uses pdfplumber when available; otherwise
plain-text resumes are supported (synthetic data and the test suite use .txt).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..inference.redaction import redact_text
from ..inference.schema import CandidateProfile
from ..matching.taxonomy import normalize_skills

_EDU_PATTERNS = [
    (r"\bph\.?d\b|\bdoctorate\b", "phd"),
    (r"\bmaster|m\.?sc|m\.?eng|mba\b", "master"),
    (r"\bbachelor|b\.?sc|b\.?eng|b\.?a\b", "bachelor"),
    (r"\bassociate\b", "associate"),
    (r"\bdiploma|certificate\b", "diploma"),
]


def infer_education_level(text: str) -> str | None:
    low = text.lower()
    for pattern, level in _EDU_PATTERNS:
        if re.search(pattern, low):
            return level
    return None


def infer_years_experience(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years?\s+(?:of\s+)?experience", text.lower())
    return float(m.group(1)) if m else 0.0


def parse_resume_text(
    candidate_id: str,
    raw_text: str,
    name: str | None = None,
    education_level: str | None = None,
    years_experience: float | None = None,
    has_resume: bool = True,
) -> CandidateProfile:
    redacted = redact_text(raw_text, name=name)
    return CandidateProfile(
        candidate_id=candidate_id,
        skills=normalize_skills(redacted),
        education_level=education_level or infer_education_level(redacted),
        years_experience=years_experience if years_experience is not None else infer_years_experience(redacted),
        text=redacted,
        has_resume=has_resume and bool(redacted.strip()),
    )


def extract_pdf_text(path: str | Path) -> str:  # pragma: no cover - optional dep
    """Best-effort PDF text extraction. Returns '' if no PDF backend is installed."""
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        try:
            from pypdf import PdfReader

            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except Exception:
            return ""
    except Exception:
        return ""


def parse_resume_file(candidate_id: str, path: str | Path, **kwargs) -> CandidateProfile:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        text = extract_pdf_text(path)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    return parse_resume_text(candidate_id, text, has_resume=bool(text.strip()), **kwargs)
