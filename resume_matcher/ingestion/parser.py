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

_EDU_PATTERNS = [
    (r"\bph\.?d\b|\bdoctorate\b", "phd"),
    (r"\bmaster|m\.?sc|m\.?eng|mba\b", "master"),
    (r"\bbachelor|b\.?sc|b\.?eng|b\.?a\b", "bachelor"),
    (r"\bassociate\b", "associate"),
    (r"\bdiploma|certificate\b", "diploma"),
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


def infer_years_experience(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years?\s+(?:of\s+)?experience", text.lower())
    return float(m.group(1)) if m else 0.0


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
) -> CandidateProfile:
    raw_text = strip_invisible(raw_text)
    # Contact identifiers (email/phone/url/address) are always redacted (cheap, and keeps the
    # non-local-adapter tripwire satisfied). The applicant's NAME is only auto-redacted when the
    # caller asks for it: the consented client demo keeps names so results stay identifiable.
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
    """Extract text from a PDF given a path or a binary file-like object. '' if no backend."""
    try:
        import pdfplumber

        with pdfplumber.open(source) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        pass
    except Exception:
        return ""
    try:
        from pypdf import PdfReader

        if hasattr(source, "seek"):
            source.seek(0)
        return "\n".join(p.extract_text() or "" for p in PdfReader(source).pages)
    except ImportError as exc:
        raise ParseError(
            "PDF support is not installed (pip install pypdf). Upload a .txt/.docx, or paste text."
        ) from exc
    except Exception:
        return ""


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
