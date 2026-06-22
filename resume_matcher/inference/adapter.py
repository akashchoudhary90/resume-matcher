"""The swappable LLM boundary.

`InferenceAdapter.extract()` is the ONE interface the rest of the system depends on. `get_adapter()`
selects an implementation from the RM_INFERENCE_BACKEND env var with no code changes. Non-local
adapters are tripwired against un-redacted input (boundary #3).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

from .redaction import assert_redacted, redact_text
from .schema import CandidateProfile, JobSpec, MatchExtraction


class InferenceError(RuntimeError):
    pass


class InferenceAdapter(ABC):
    """Implementations turn a (candidate, job) pair into a schema-valid MatchExtraction.

    `is_local` flags whether resume text leaves the machine. Anything non-local must only ever
    receive redacted text — enforced by `extract()` below.

    `transmits_offbox` flags a backend that sends resume text to a REMOTE provider even though it is
    "governed by you" (e.g. the Claude subscription CLI). Such backends are not `is_local=False`
    (the consented demo deliberately sends the resume body + name), but `extract()` still strips
    direct CONTACT identifiers before transmission — a backstop so a caller that forgot to redact
    can never leak an email/phone/url/address off the box (boundary #3).
    """

    name: str = "base"
    is_local: bool = True
    transmits_offbox: bool = False

    @abstractmethod
    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        ...

    def extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        if self.transmits_offbox:
            # Mandatory contact-only redaction before anything leaves the box. Done on a COPY so the
            # caller's candidate (and the text the deterministic ranker verifies against) is unchanged.
            safe = redact_text(candidate.text or "", name=None)
            if safe != (candidate.text or ""):
                candidate = candidate.model_copy(update={"text": safe})
        elif not self.is_local:
            leaks = assert_redacted(candidate.text)
            if leaks:
                raise InferenceError(
                    f"Refusing to send un-redacted PII ({', '.join(leaks)}) to non-local "
                    f"adapter '{self.name}'. Run inference/redaction.redact_text first."
                )
        result = self._extract(candidate, job)
        # Pin ids — never trust a backend (or an injection) to set them.
        result.candidate_id = candidate.candidate_id
        result.job_id = job.job_id
        return result


def _balanced_object(text: str) -> str | None:
    """Return the first brace-balanced {...} object in `text`, tracking string literals so braces
    inside quoted values don't count. Returns None if no balanced object is found. This is more
    robust than first-'{' .. last-'}', which over-captures when prose with braces trails the JSON."""
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_object(raw: str) -> dict:
    """Parse the JSON object out of a model's raw text. Tolerates code fences and surrounding prose;
    uses brace-balancing (not last-'}') so trailing text with braces doesn't break it. Raises
    InferenceError when no valid JSON object can be recovered. Shared by every adapter parser."""
    text = (raw or "").strip()
    if "```" in text:  # strip markdown code fences if present
        parts = text.split("```")
        text = max(parts, key=len).removeprefix("json").strip()
    blob = _balanced_object(text)
    if blob is None:  # last-ditch fallback (e.g. truncated output): first-'{' .. last-'}'
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise InferenceError(f"No JSON object found in model output: {raw[:200]!r}")
        blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        raise InferenceError(f"Model output was not valid JSON: {exc}") from exc


def parse_extraction(raw: str, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
    """Parse a model's raw text into a validated MatchExtraction. Tolerates JSON wrapped in prose /
    code fences. Raises InferenceError on unrecoverable output."""
    data = extract_json_object(raw)
    data.setdefault("candidate_id", candidate.candidate_id)
    data.setdefault("job_id", job.job_id)
    try:
        return MatchExtraction.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise InferenceError(f"Model output failed schema validation: {exc}") from exc


def get_adapter(backend: str | None = None) -> InferenceAdapter:
    """Factory. RM_INFERENCE_BACKEND in {mock, claude_code, claude_cli, ollama, openai_compat}; default mock."""
    backend = (backend or os.environ.get("RM_INFERENCE_BACKEND", "mock")).lower()
    if backend == "mock":
        from .adapters.mock import MockAdapter

        return MockAdapter()
    if backend == "claude_code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter()
    if backend == "claude_cli":
        from .adapters.claude_cli import ClaudeCliAdapter

        return ClaudeCliAdapter()
    if backend == "ollama":
        from .adapters.ollama import OllamaAdapter

        return OllamaAdapter()
    if backend == "openai_compat":
        from .adapters.openai_compat import OpenAICompatAdapter

        return OpenAICompatAdapter()
    raise InferenceError(
        f"Unknown RM_INFERENCE_BACKEND={backend!r}. "
        f"Expected one of: mock, claude_code, claude_cli, ollama, openai_compat."
    )
