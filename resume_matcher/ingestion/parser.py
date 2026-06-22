"""Resume parsing -> structured CandidateProfile.

Redaction happens HERE, at ingestion, so PII never propagates downstream (boundary #3). Skills are
normalized to canonical IDs via the taxonomy. PDF parsing uses pdfplumber/pypdf when available and
.docx uses python-docx; otherwise plain-text resumes are supported (synthetic data and the test
suite use .txt).

For the ephemeral client demo, `parse_resume_bytes` parses an uploaded file ENTIRELY IN MEMORY —
the bytes are never written to disk (privacy requirement for real-data demos).
"""
from __future__ import annotations

import io
import re
import unicodedata
from pathlib import Path

from ..inference.redaction import redact_text
from ..inference.schema import CandidateProfile
from ..matching.taxonomy import normalize_skills

TEXT_EXTS = {".txt", ".text", ".md", ""}
SUPPORTED_EXTS = TEXT_EXTS | {".pdf", ".docx"}

# Each alternative is individually bounded with \b...\b. Without per-alternative boundaries the | binds
# looser than concatenation, so e.g. "scuba" matched b\.?a\b -> bachelor and "samba" matched mba\b.
_EDU_PATTERNS = [
    (r"\b(?:ph\.?d|doctorate)\b", "phd"),
    (r"\b(?:master|m\.?sc|m\.?eng|mba)\b", "master"),
    (r"\b(?:bachelor|b\.?sc|b\.?eng|b\.?a)\b", "bachelor"),
    (r"\bassociate\b", "associate"),
    (r"\b(?:diploma|certificate)\b", "diploma"),
]

# A name-shaped header line: 2-4 capitalized tokens, no digits/@.
_NAME_LINE_RE = re.compile(r"^[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-.]+){1,3}$")


def strip_invisible(text: str) -> str:
    """Remove invisible / format / control characters (zero-width, bidi overrides, the Unicode Tags
    block, etc.) that can smuggle hidden instructions or evade keyword checks. Keeps \\n \\t \\r.

    This neutralizes the smuggling channel at ingestion so hidden characters never reach the resume
    text, the evidence quotes, or any (non-mock) LLM prompt — complementing the advisory
    zero-width detection in antigaming/injection.py."""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in ("\n", "\t", "\r"):
            out.append(ch)
            continue
        cp = ord(ch)
        if 0xE0000 <= cp <= 0xE007F:  # Unicode Tags block (ASCII smuggling)
            continue
        if unicodedata.category(ch) in ("Cf", "Cc"):  # format / control
            continue
        out.append(ch)
    return "".join(out)


def infer_education_level(text: str) -> str | None:
    low = text.lower()
    for pattern, level in _EDU_PATTERNS:
        if re.search(pattern, low):
            return level
    return None


_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}
# "X years/yrs" (digit or spelled), optional "+", not followed by "ago"/"old" (those aren't tenure).
_YEARS_DIGIT = re.compile(r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b(?!\s+(?:ago|old))")
_YEARS_WORD = re.compile(
    r"\b(" + "|".join(_NUM_WORDS) + r")\s*\+?\s*(?:years?|yrs?)\b(?!\s+(?:ago|old))"
)


def infer_years_experience(text: str) -> float:
    """Best-effort total years of experience. Handles digits and spelled-out numbers, "X+ years",
    and "X years as a ..." (not just "X years of experience"); ignores "X years ago/old". Returns the
    LARGEST plausible figure mentioned (a recruiter reads total tenure)."""
    low = (text or "").lower()
    yrs = [float(m.group(1)) for m in _YEARS_DIGIT.finditer(low)]
    yrs += [float(_NUM_WORDS[m.group(1)]) for m in _YEARS_WORD.finditer(low)]
    return max(yrs) if yrs else 0.0


def guess_name(text: str) -> str | None:
    """Best-effort: treat the first short, name-shaped header line as the applicant's name, so the
    redaction chokepoint can strip it even when the caller has no name (e.g. an uploaded resume with
    no accompanying metadata). Conservative: only the first non-empty line, and only if it has no
    digits/@ and looks like 2-4 capitalized words."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line or any(ch.isdigit() for ch in line):
            return None  # first real line is contact info, not a name
        return line if _NAME_LINE_RE.match(line) else None
    return None


def parse_resume_text(
    candidate_id: str,
    raw_text: str,
    name: str | None = None,
    education_level: str | None = None,
    years_experience: float | None = None,
    has_resume: bool = True,
    auto_redact_name: bool = True,
    redact: bool = True,
) -> CandidateProfile:
    # Invisible/zero-width characters are stripped regardless (anti-injection hygiene, not PII).
    raw_text = strip_invisible(raw_text)
    if not redact:
        # Caller explicitly wants the raw text (e.g. the consented client demo): no PII redaction.
        redacted = raw_text
    else:
        # Contact identifiers (email/phone/url/address) are redacted; the applicant's NAME is only
        # auto-redacted when the caller asks for it.
        redact_name = name or (guess_name(raw_text) if auto_redact_name else None)
        redacted = redact_text(raw_text, name=redact_name)
    return CandidateProfile(
        candidate_id=candidate_id,
        skills=normalize_skills(redacted),
        education_level=education_level or infer_education_level(redacted),
        years_experience=years_experience if years_experience is not None else infer_years_experience(redacted),
        text=redacted,
        has_resume=has_resume and bool(redacted.strip()),
    )


class ParseError(Exception):
    """Raised when an uploaded file cannot be turned into text (unsupported type or missing parser
    backend). The demo API turns this into a clear, per-file message rather than a silent empty."""


def _extract_pdf(source) -> str:
    """Extract text from a PDF (path or binary file-like). Tries pdfplumber (best), then pypdf.

    Returns '' when the PDF has no extractable text layer (e.g. a scanned/photo PDF) — the caller
    surfaces that as a clear 'no readable text' message. Raises ParseError only when NO PDF backend
    is installed at all."""
    text = ""
    had_backend = False
    try:
        import pdfplumber

        had_backend = True
        if hasattr(source, "seek"):
            source.seek(0)
        with pdfplumber.open(source) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        pass
    except Exception:  # pdfplumber choked on this file — fall through to pypdf
        text = ""
    if text.strip():
        return text

    # Fall back to pypdf (also the path when pdfplumber isn't installed, or returned nothing).
    try:
        from pypdf import PdfReader

        had_backend = True
        if hasattr(source, "seek"):
            source.seek(0)
        text2 = "\n".join(p.extract_text() or "" for p in PdfReader(source).pages)
        if text2.strip():
            return text2
    except ImportError:
        pass
    except Exception:
        pass

    if not had_backend:
        raise ParseError(
            "PDF support is not installed (pip install pdfplumber pypdf). "
            "Upload a .txt/.docx instead."
        )
    return text  # possibly '' -> scanned image / no text layer


def _extract_docx(source) -> str:
    """Extract text from a .docx given a path or a binary file-like object. Requires python-docx."""
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise ParseError(
            "DOCX support is not installed (pip install python-docx). Upload a .txt/.pdf, or paste text."
        ) from exc
    try:
        document = docx.Document(source)
    except Exception as exc:  # corrupt / not a real docx
        raise ParseError("Could not read this .docx file (it may be corrupt).") from exc
    return "\n".join(p.text for p in document.paragraphs)


def extract_pdf_text(path: str | Path) -> str:  # pragma: no cover - optional dep
    """Best-effort PDF text extraction from a path. Returns '' if no PDF backend is installed."""
    try:
        return _extract_pdf(str(path))
    except ParseError:
        return ""


def extract_file_text(path: str | Path) -> str:
    """Read any supported resume file (.pdf/.docx/.txt) from disk into plain text."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(str(path))
    if ext == ".docx":
        return _extract_docx(str(path))
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_bytes_text(filename: str, data: bytes) -> str:
    """Turn uploaded file bytes into plain text IN MEMORY (never touches disk). Raises ParseError on
    an unsupported type or a missing parser backend."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(io.BytesIO(data))
    if ext == ".docx":
        return _extract_docx(io.BytesIO(data))
    if ext in TEXT_EXTS:
        return data.decode("utf-8", errors="ignore")
    if ext == ".doc":
        raise ParseError("Legacy .doc isn't supported — please upload .pdf, .docx, or .txt.")
    # Unknown extension: try to decode as text rather than fail outright.
    return data.decode("utf-8", errors="ignore")


def parse_resume_file(candidate_id: str, path: str | Path, **kwargs) -> CandidateProfile:
    text = extract_file_text(path)
    return parse_resume_text(candidate_id, text, has_resume=bool(text.strip()), **kwargs)


def parse_resume_bytes(
    candidate_id: str, filename: str, data: bytes, **kwargs
) -> CandidateProfile:
    """Parse an uploaded resume from raw bytes, fully in memory. Redaction runs inside
    parse_resume_text, so PII is stripped before the profile leaves this function."""
    text = extract_bytes_text(filename, data)
    return parse_resume_text(candidate_id, text, has_resume=bool(text.strip()), **kwargs)
