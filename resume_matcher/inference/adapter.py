"""The swappable LLM boundary.

`InferenceAdapter.extract()` is the ONE interface the rest of the system depends on. `get_adapter()`
selects an implementation from the RM_INFERENCE_BACKEND env var with no code changes. Non-local
adapters are tripwired against un-redacted input (boundary #3).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

from .redaction import assert_redacted
from .schema import CandidateProfile, JobSpec, MatchExtraction


class InferenceError(RuntimeError):
    pass


class InferenceAdapter(ABC):
    """Implementations turn a (candidate, job) pair into a schema-valid MatchExtraction.

    `is_local` flags whether resume text leaves the machine. Anything non-local must only ever
    receive redacted text — enforced by `extract()` below.
    """

    name: str = "base"
    is_local: bool = True

    @abstractmethod
    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        ...

    def extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        if not self.is_local:
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


def parse_extraction(raw: str, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
    """Parse a model's raw text into a validated MatchExtraction. Tolerates JSON wrapped in prose /
    code fences. Raises InferenceError on unrecoverable output."""
    text = raw.strip()
    if "```" in text:  # strip markdown code fences if present
        parts = text.split("```")
        text = max(parts, key=len).removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise InferenceError(f"No JSON object found in model output: {raw[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise InferenceError(f"Model output was not valid JSON: {exc}") from exc
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
