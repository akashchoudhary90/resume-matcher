"""JD-autofill orchestrator: P0 → P5 (docs/JD_AUTOFILL.md §2). One call turns a pasted/uploaded JD
into a reviewable JobPosting draft with per-field provenance.

Fail-open by design: the LLM pass (P4) is best-effort — any failure yields the deterministic
P1+P2 draft flagged `llm_unavailable`, so the form still pre-fills what regexes and the taxonomy
found.

Backends (Slice N — the isolated-deployment answer): `claude_cli` (local CLI on the owner's
subscription), `ollama` (fully on-box, grammar-constrained via the pinned schema — the York
isolation mode), `openai_compat` (local vLLM/llama-server, or hosted). Boundary #3 is enforced in
code here: a NON-LOCAL backend only ever receives a `redact_text`-ed JD copy, gated by
`assert_redacted` — and P2 has already captured contact/application fields deterministically
BEFORE that, so redaction costs the form nothing (the JD_AUTOFILL P0 fork, made real).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict

from ..inference.adapters import claude_cli as _claude_cli
from ..inference.posting_schema import (
    FieldStatus,
    JobPosting,
    PostingExtraction,
    posting_extraction_schema,
)
from ..inference.redaction import assert_redacted, redact_text
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


def _posting_messages(text: str, title: str, only_role: str) -> list[dict]:
    """The same fenced, no-authority prompt claude_cli uses, as chat messages for chat backends."""
    role_line = f"Extract ONLY the role titled: {only_role}\n\n" if only_role else ""
    user = (f"{role_line}Job title hint: {title or '(none)'}\n\n"
            f"{_claude_cli._JD_FENCE}\n{text}\n{_claude_cli._JD_FENCE}\n")
    return [{"role": "system", "content": _claude_cli._POSTING_SYSTEM},
            {"role": "user", "content": user}]


def _ollama_extract_posting(text: str, title: str = "", only_role: str = "") -> dict:
    """Fully on-box: Ollama structured-output mode, grammar-constrained by the pinned schema."""
    import ollama

    host = os.environ.get("RM_OLLAMA_HOST")
    client = ollama.Client(host=host) if host else ollama
    resp = client.chat(
        model=os.environ.get("RM_OLLAMA_MODEL", "qwen2.5:7b-instruct"),
        messages=_posting_messages(text, title, only_role),
        format=posting_extraction_schema(),
        options={"temperature": 0},
    )
    return json.loads(resp["message"]["content"])


def _openai_extract_posting(text: str, title: str = "", only_role: str = "") -> dict:
    """OpenAI-compatible endpoint (local vLLM/llama-server, or hosted)."""
    from openai import OpenAI

    client = OpenAI(base_url=os.environ.get("RM_OPENAI_BASE_URL"),
                    api_key=os.environ.get("RM_OPENAI_API_KEY", "not-needed-for-local"))
    resp = client.chat.completions.create(
        model=os.environ.get("RM_OPENAI_MODEL", "gpt-4o-mini"),
        messages=_posting_messages(text, title, only_role),
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _openai_is_local() -> bool:
    base = os.environ.get("RM_OPENAI_BASE_URL") or ""
    return any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


# backend -> (callable(text, title, only_role) -> raw dict, is_local() -> bool).
# Tests monkeypatch entries; api/platform.py picks the backend via RM_PLATFORM_EXTRACT_BACKEND.
_BACKEND_CALLS: dict[str, tuple] = {
    "claude_cli": (lambda text, title, only_role:
                   _claude_cli.extract_posting(text, title=title, only_role=only_role),
                   lambda: True),  # local CLI on the owner's subscription (is_local by design)
    "ollama": (_ollama_extract_posting, lambda: True),
    "openai_compat": (_openai_extract_posting, _openai_is_local),
}


def _llm_posting_extraction(text: str, title_hint: str, backend: str | None,
                            only_role: str = "") -> tuple[PostingExtraction | None, list[str]]:
    """P4. Returns (validated extraction | None, extra flags)."""
    entry = _BACKEND_CALLS.get(backend or "")
    if entry is None or (backend == "claude_cli" and not _claude_cli.available()):
        return None, ["llm_unavailable"]
    call, is_local = entry

    # Boundary #3: a non-local backend only ever sees a redacted JD copy. P2 already captured
    # contact/application fields deterministically, so the form loses nothing.
    outbound = text
    if not is_local():
        outbound = redact_text(text)
        if assert_redacted(outbound):
            _log.warning("redaction tripwire: JD still leaks identifiers; refusing non-local call")
            return None, ["llm_unavailable"]

    key = hashlib.sha256(
        "\x00".join([backend or "", title_hint.lower().strip(), only_role.lower(),
                     outbound]).encode("utf-8")
    ).hexdigest()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
    if cached is not None:
        return (PostingExtraction.model_validate(cached) if cached else None), (
            [] if cached else ["llm_unavailable"])
    try:
        raw = call(outbound, title_hint, only_role)
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
    model = None
    if llm is not None:
        model = _claude_cli.model_name() if backend == "claude_cli" else (
            os.environ.get("RM_OLLAMA_MODEL", "qwen2.5:7b-instruct") if backend == "ollama"
            else os.environ.get("RM_OPENAI_MODEL", "gpt-4o-mini"))
        model = f"{backend}:{model}"
    draft.extraction_meta = {
        "source": "file" if file_bytes is not None else "pasted",
        "filename": filename or None,
        "source_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "model": model,
        "language": llm.language if llm is not None else
        ("fr" if "non_english:fr" in flags else "en"),
        "other_role_titles": llm.other_role_titles if llm is not None else [],
        "flags": flags,
    }
    return draft
