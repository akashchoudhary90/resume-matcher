"""Claude backend via the local Claude Code CLI on your SUBSCRIPTION — no API key, no per-token bill.

Same pattern the Kotak trading project uses: shell out to `claude -p` (headless print mode)
authenticated with the owner's subscription token (`claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN).

Two modes:
  * extract()        — text mode: the resume's extracted text is in the prompt (fast, cheap).
  * extract_from_file() — FILE-DIRECT mode: Claude reads the actual PDF/image natively (vision), so
    scanned/photo resumes work and layout is preserved. It returns Claude's full transcription
    (`resume_text`) too, so the deterministic ranker can still verify every evidence quote is a
    verbatim substring of what Claude read.

Privilege separation is unchanged either way: Claude only EXTRACTS; matching/ranker.py decides the
score and discards any quote it can't verify, so a hallucinated skill cannot move the number.

Inert unless enabled: needs the `claude` CLI on PATH AND CLAUDE_CODE_OAUTH_TOKEN set.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading

from ..adapter import InferenceAdapter, InferenceError, parse_extraction
from ..prompt import build_messages
from ..schema import CandidateProfile, JobSpec, MatchExtraction, match_extraction_schema

_MODEL = os.environ.get("RM_CLAUDE_CLI_MODEL", "opus")  # most capable; override to sonnet/haiku for speed
_TIMEOUT_S = float(os.environ.get("RM_CLAUDE_CLI_TIMEOUT", "90"))
_FILE_TIMEOUT_S = float(os.environ.get("RM_CLAUDE_CLI_FILE_TIMEOUT", "150"))  # vision is slower
_MAX_CONCURRENCY = max(1, int(os.environ.get("RM_CLAUDE_CLI_CONCURRENCY", "4") or "4"))
_SEM = threading.Semaphore(_MAX_CONCURRENCY)

# File types Claude can read directly (native PDF + vision). .docx/.txt stay on text extraction.
FILE_DIRECT_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Locked-down flags for pure text generation (no tools, no MCP, single turn).
_TEXT_ARGS = ["--tools", "", "--strict-mcp-config", "--permission-mode", "plan", "--max-turns", "1"]
# File mode needs the Read tool to open the attached resume. Read is the ONLY allowed tool, so even a
# permissive mode can do nothing but read. These are the most likely-correct flags for headless file
# reads; tweak via the source if a CLI version differs (failures fall back to text extraction).
_FILE_ARGS = ["--allowedTools", "Read", "--strict-mcp-config", "--permission-mode", "acceptEdits",
              "--max-turns", "6"]


def available() -> bool:
    """True only when the CLI is installed AND a subscription token is present."""
    return bool(shutil.which("claude")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def supports_file(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in FILE_DIRECT_EXTS


def _flatten(messages: list[dict]) -> str:
    return "\n\n".join(m["content"] for m in messages)


def _run_cli(prompt: str, *, extra_args: list[str], cwd: str | None, timeout: float) -> str:
    """Spawn `claude -p` (headless) and return raw stdout."""
    exe = shutil.which("claude")
    if not exe:
        raise InferenceError("claude CLI not on PATH (install it, or use RM_DEMO_BACKEND=mock).")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise InferenceError("CLAUDE_CODE_OAUTH_TOKEN not set (run `claude setup-token`).")
    argv = [exe, "-p", prompt, "--model", _MODEL, "--output-format", "text", *extra_args]
    with _SEM:
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout, cwd=cwd or tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as exc:
            raise InferenceError(f"claude -p timed out after {timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise InferenceError("claude CLI not found") from exc
    if proc.returncode != 0:
        raise InferenceError(f"claude -p exited {proc.returncode}: {(proc.stderr or '')[:300]}")
    return proc.stdout or ""


def _job_block(job: JobSpec) -> str:
    return (
        f"JOB\n"
        f"  job_id: {job.job_id}\n"
        f"  title: {job.title}\n"
        f"  employer: {job.employer}\n"
        f"  required_skills (canonical ids): {job.required_skills}\n"
        f"  preferred_skills (canonical ids): {job.preferred_skills}\n"
        f"  min_education: {job.min_education}\n"
        f"  description: {job.description}\n"
    )


def _build_file_prompt(job: JobSpec, candidate_id: str, file_basename: str) -> str:
    from ..prompt import SYSTEM

    schema = json.dumps(match_extraction_schema(), indent=2)
    return (
        f"{SYSTEM}\n\n"
        f"{_job_block(job)}\n"
        f"The candidate's resume is the attached file: @{file_basename}\n"
        f"Read the ENTIRE resume, including any scanned or image-only pages.\n\n"
        f"Return ONLY one JSON object with these fields:\n"
        f"  - resume_text: the full plain-text transcription of everything you can read in the file\n"
        f"  - the MatchExtraction fields conforming to this JSON Schema:\n{schema}\n"
        f"Use candidate_id='{candidate_id}' and job_id='{job.job_id}'.\n"
        f"EVIDENCE RULE: every match/partial evidence_span MUST be a verbatim quote copied from "
        f"resume_text. No prose outside the JSON."
    )


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len).removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise InferenceError(f"No JSON object in Claude output: {raw[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise InferenceError(f"Claude output was not valid JSON: {exc}") from exc


def extract_from_file(file_path: str, job: JobSpec, candidate_id: str) -> tuple[str, MatchExtraction]:
    """Have Claude read the resume FILE directly and return (resume_text, MatchExtraction).

    `resume_text` is Claude's transcription — the ranker verifies evidence spans against it."""
    cwd = os.path.dirname(os.path.abspath(file_path))
    base = os.path.basename(file_path)
    raw = _run_cli(
        _build_file_prompt(job, candidate_id, base),
        extra_args=_FILE_ARGS, cwd=cwd, timeout=_FILE_TIMEOUT_S,
    )
    data = _parse_json_object(raw)
    resume_text = str(data.pop("resume_text", "") or "")
    data.setdefault("candidate_id", candidate_id)
    data.setdefault("job_id", job.job_id)
    try:
        extraction = MatchExtraction.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise InferenceError(f"Claude file output failed schema validation: {exc}") from exc
    extraction.candidate_id = candidate_id
    extraction.job_id = job.job_id
    return resume_text, extraction


class ClaudeCliAdapter(InferenceAdapter):
    name = "claude_cli"
    is_local = True  # runs through YOUR Claude session (subscription), governed by you

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        raw = _run_cli(
            _flatten(build_messages(candidate, job)),
            extra_args=_TEXT_ARGS, cwd=None, timeout=_TIMEOUT_S,
        )
        return parse_extraction(raw, candidate, job)
