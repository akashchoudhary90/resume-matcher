"""OpenAI-compatible /v1/chat/completions backend.

Serves a local engine now (vLLM, llama-server, LM Studio — all expose this API) OR a hosted model
later. Swapping between them is a base_url change, not a code change. If base_url is not localhost,
the adapter is treated as NON-LOCAL and the redaction tripwire in InferenceAdapter.extract applies.
"""
from __future__ import annotations

import os

from ..adapter import InferenceAdapter, InferenceError, parse_extraction
from ..prompt import build_messages
from ..schema import CandidateProfile, JobSpec, MatchExtraction


def _looks_local(base_url: str | None) -> bool:
    if not base_url:
        return False
    return any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


class OpenAICompatAdapter(InferenceAdapter):
    name = "openai_compat"

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = base_url or os.environ.get("RM_OPENAI_BASE_URL")
        self.model = model or os.environ.get("RM_OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("RM_OPENAI_API_KEY", "not-needed-for-local")
        # Hosted endpoints see resume text leave the machine -> non-local -> redaction enforced.
        self.is_local = _looks_local(self.base_url)

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise InferenceError(
                "openai package not installed. `pip install -r requirements-extra.txt` or use "
                "RM_INFERENCE_BACKEND=mock."
            ) from exc

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        resp = client.chat.completions.create(
            model=self.model,
            messages=build_messages(candidate, job),
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        return parse_extraction(raw, candidate, job)
