"""Default dev backend: local Claude Code via MCP `sampling/createMessage`.

This adapter has no model SDK. When the scoring code runs *inside* the stdio MCP server
(inference/server.py), the MCP host (Claude Code) provides a sampler callable that the server
registers here via `set_sampler`. Standalone (no host), there is no local model to call, so it
raises a clear InferenceError pointing you to the MCP server or RM_INFERENCE_BACKEND=mock.

Keeping the model behind MCP sampling means this server bundles zero model SDK and stays fully
model-independent — the receiving team swaps the backend without touching this file.
"""
from __future__ import annotations

from collections.abc import Callable

from ..adapter import InferenceAdapter, InferenceError, parse_extraction
from ..prompt import build_messages
from ..schema import CandidateProfile, JobSpec, MatchExtraction

# A sampler maps chat messages -> raw assistant text. Injected by the MCP host at runtime.
Sampler = Callable[[list[dict]], str]
_SAMPLER: Sampler | None = None


def set_sampler(sampler: Sampler | None) -> None:
    """Called by inference/server.py once the MCP sampling capability is available."""
    global _SAMPLER
    _SAMPLER = sampler


class ClaudeCodeAdapter(InferenceAdapter):
    name = "claude_code"
    is_local = True  # inference happens on the host running Claude Code; PII does not leave it.

    def __init__(self, sampler: Sampler | None = None):
        self._sampler = sampler

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        sampler = self._sampler or _SAMPLER
        if sampler is None:
            raise InferenceError(
                "ClaudeCodeAdapter has no sampler. Run scoring through the MCP server "
                "(inference/server.py) so Claude Code can provide sampling, or set "
                "RM_INFERENCE_BACKEND=mock for an offline run."
            )
        raw = sampler(build_messages(candidate, job))
        return parse_extraction(raw, candidate, job)
