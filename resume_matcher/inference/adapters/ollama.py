"""Local open-model backend via Ollama. Fully offline — PII never leaves the machine.

Uses Ollama's `format=<json schema>` structured-output mode at temperature 0 so the model is
grammar-constrained and physically cannot emit invalid JSON. Pick the model with RM_OLLAMA_MODEL.
"""
from __future__ import annotations

import os

from ..adapter import InferenceAdapter, InferenceError, parse_extraction
from ..prompt import build_messages
from ..schema import CandidateProfile, JobSpec, MatchExtraction, match_extraction_schema


class OllamaAdapter(InferenceAdapter):
    name = "ollama"
    is_local = True

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("RM_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        self.host = host or os.environ.get("RM_OLLAMA_HOST")  # default localhost:11434

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        try:
            import ollama
        except ImportError as exc:  # pragma: no cover - optional dep
            raise InferenceError(
                "ollama package not installed. `pip install -r requirements-extra.txt` or use "
                "RM_INFERENCE_BACKEND=mock."
            ) from exc

        client = ollama.Client(host=self.host) if self.host else ollama
        messages = build_messages(candidate, job)
        resp = client.chat(
            model=self.model,
            messages=messages,
            format=match_extraction_schema(),  # structured output: grammar-constrained JSON
            options={"temperature": 0},
        )
        raw = resp["message"]["content"]
        return parse_extraction(raw, candidate, job)
