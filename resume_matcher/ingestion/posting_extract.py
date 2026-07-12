"""JD-autofill orchestrator: P0 → P5 (docs/JD_AUTOFILL.md §2). One call turns a pasted/uploaded JD
into a reviewable JobPosting draft with per-field provenance.

Fail-open by design: the LLM pass (P4) is claude_cli-only and best-effort — any failure yields the
deterministic P1+P2 draft flagged `llm_unavailable`, so the form still pre-fills what regexes and
the taxonomy found. Boundary #3 note: the LLM path currently runs only on the LOCAL Claude CLI
adapter; if a non-local backend is ever wired here, the JD copy it sees must pass redact_text +
assert_redacted first (contacts are captured deterministically in P2 before that, so redaction
costs nothing — see JD_AUTOFILL P0).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict

from ..inference.adapters import claude_cli as _claude_cli
from ..inference.posting_schema import FieldStatus, JobPosting, PostingExtraction
from .jd_fields import (
    anomaly_flags,
    extract_bullets,
    extract_contacts,
    extract_dates,
    extract_min_requirements,
    extract_pay,
    extract_skills,
    extract_title,
    extract_type_and_mode,
)
from .jd_merge import merge_draft
from .jd_structure import sectionize
from .parser import extract_bytes_text, strip_invisible

_log = logging.getLogger("resume_matcher.ingestion.posting_extract")


class PostingExtractError(Exception):
    """A client-correctable problem (empty/unreadable JD) -> HTTP 400 at the route."""


# Small LRU for LLM posting extractions (consistency + cost, same semantics as the demo's cache:
# empty results cached as negatives, FAILURES stay uncached so transient errors retry).
_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 128


def _llm_posting_extraction(text: str, title_hint: str, backend: str | None,
                            only_role: str = "") -> tuple[PostingExtraction | None, list[str]]:
    """P4. Returns (validated extraction | None, extra flags)."""
    if backend != "claude_cli" or not _claude_cli.available():
        return None, ["llm_unavailable"]
    key = hashlib.sha256(
        "\x00".join([_claude_cli.model_name(), title_hint.lower().strip(), only_role.lower(),
                     text]).encode("utf-8")
    ).hexdigest()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
    if cached is not None:
        return (PostingExtraction.model_validate(cached) if cached else None), (
            [] if cached else ["llm_unavailable"])
    try:
        raw = _claude_cli.extract_posting(text, title=title_hint, only_role=only_role)
        extraction = _validate_salvaging(raw)
    except Exception:  # noqa: BLE001 - P4 is best-effort; the deterministic draft still ships
        _log.warning("LLM posting extraction failed; serving deterministic draft", exc_info=True)
        return None, ["llm_unavailable"]
    with _CACHE_LOCK:
        _CACHE[key] = extraction.model_dump(mode="json") if extraction else {}
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    if extraction is None:
        return None, ["llm_unavailable"]
    return extraction, []


def _validate_salvaging(raw: dict) -> PostingExtraction | None:
    """Validate against the pinned contract; on a partial mismatch drop the offending top-level
    keys and retry once (per-field salvage — a bad pay object shouldn't void a good title)."""
    from pydantic import ValidationError

    if not isinstance(raw, dict):
        return None
    try:
        return PostingExtraction.model_validate(raw)
    except ValidationError as exc:
        bad_keys = {err["loc"][0] for err in exc.errors() if err.get("loc")}
        salvaged = {k: v for k, v in raw.items() if k not in bad_keys}
        try:
            return PostingExtraction.model_validate(salvaged)
        except ValidationError:
            return None


def extract_posting_draft(
    *,
    text: str | None = None,
    file_bytes: bytes | None = None,
    filename: str = "",
    backend: str | None = None,
    title_hint: str = "",
    only_role: str = "",
) -> JobPosting:
    """The full P0→P5 pipeline. Give it pasted `text` OR (`file_bytes`, `filename`)."""
    if file_bytes is not None:
        text = extract_bytes_text(filename, file_bytes)  # in-memory; ParseError is client-facing
    canonical = strip_invisible(text or "")
    if not canonical.strip():
        raise PostingExtractError(
            "No readable text in the posting. Paste the text, or upload a .pdf/.docx/.txt with a "
            "text layer (scanned PDFs aren't supported here yet)."
        )

    sections = sectionize(canonical)                                   # P1
    det_fields = {}                                                    # P2
    det_fields.update(extract_contacts(canonical))
    det_fields.update(extract_dates(canonical))
    det_fields.update(extract_type_and_mode(canonical, sections))
    det_fields.update(extract_min_requirements(canonical, sections))
    det_fields.update(extract_bullets(canonical, sections))
    pay = extract_pay(canonical, sections)
    if pay:
        det_fields["pay"] = pay
    title = extract_title(canonical, sections)
    if title:
        det_fields["title"] = title
    det_skills = extract_skills(canonical, sections)

    flags = anomaly_flags(canonical)                                   # P3
    llm, llm_flags = _llm_posting_extraction(canonical, title_hint or (title.value if title else ""),
                                             backend, only_role)       # P4
    flags += llm_flags

    draft = merge_draft(canonical, det_fields, det_skills, llm, flags)  # P5

    if llm is not None and llm.multi_role_detected and "multi_role_suspected" not in flags:
        flags.append("multi_role_suspected")
    if "multi_role_suspected" in flags:
        draft.title.status = FieldStatus.conflict  # never silently merge roles — the human picks
    draft.extraction_meta = {
        "source": "file" if file_bytes is not None else "pasted",
        "filename": filename or None,
        "source_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "model": _claude_cli.model_name() if llm is not None else None,
        "language": llm.language if llm is not None else
        ("fr" if "non_english:fr" in flags else "en"),
        "other_role_titles": llm.other_role_titles if llm is not None else [],
        "flags": flags,
    }
    return draft
