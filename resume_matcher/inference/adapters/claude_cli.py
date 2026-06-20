"""Claude backend via the local Claude Code CLI on your SUBSCRIPTION — no API key, no per-token bill.

Same pattern the Kotak trading project uses (live_trading/services/llm_translate.py): shell out to
`claude -p` (headless print mode) authenticated with the owner's subscription token
(`claude setup-token` -> CLAUDE_CODE_OAUTH_TOKEN). Usage draws on the Claude plan; nothing here needs
an API key.

Privilege separation is unchanged: this adapter only does structured EXTRACTION (which skills are
evidenced, with verbatim quotes). matching/ranker.py still makes the deterministic scoring decision
and discards any claimed skill whose quote isn't a real substring of the resume — so a hallucinated
or injected skill cannot move the score.

Inert unless enabled: needs the `claude` CLI on PATH AND CLAUDE_CODE_OAUTH_TOKEN set. When either is
missing, `available()` is False and the demo falls back to the deterministic mock backend.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading

from ..adapter import InferenceAdapter, InferenceError, parse_extraction
from ..prompt import build_messages
from ..schema import CandidateProfile, JobSpec, MatchExtraction

# Small fast model is the default ("sonnet" for richer matching; set RM_CLAUDE_CLI_MODEL=haiku for
# speed). The CLI accepts these aliases or full model ids.
_MODEL = os.environ.get("RM_CLAUDE_CLI_MODEL", "sonnet")
_TIMEOUT_S = float(os.environ.get("RM_CLAUDE_CLI_TIMEOUT", "90"))
# Hard cap on concurrent `claude` processes across the whole app, regardless of how many requests
# fan out — a burst of uploads must not fork-bomb the box (cf. Kotak's Semaphore).
_MAX_CONCURRENCY = max(1, int(os.environ.get("RM_CLAUDE_CLI_CONCURRENCY", "4") or "4"))
_SEM = threading.Semaphore(_MAX_CONCURRENCY)


def available() -> bool:
    """True only when the CLI is installed AND a subscription token is present."""
    return bool(shutil.which("claude")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def _flatten(messages: list[dict]) -> str:
    """Combine the chat messages into one prompt string for `claude -p` (mirrors Kotak's single
    -p prompt; the security/anti-injection system text is preserved at the top)."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            parts.append(m["content"])
        else:
            parts.append(m["content"])
    return "\n\n".join(parts)


def _run_cli(prompt: str) -> str:
    """Spawn `claude -p` (headless) and return raw stdout. Locked down to pure text generation:
    no tools, no MCP servers, plan permission-mode, single turn, scratch cwd."""
    exe = shutil.which("claude")
    if not exe:
        raise InferenceError("claude CLI not on PATH (install it, or use RM_DEMO_BACKEND=mock).")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise InferenceError(
            "CLAUDE_CODE_OAUTH_TOKEN not set. Run `claude setup-token` and put it in the env."
        )
    argv = [
        exe, "-p", prompt,
        "--model", _MODEL,
        "--output-format", "text",
        "--tools", "",
        "--strict-mcp-config",
        "--permission-mode", "plan",
        "--max-turns", "1",
    ]
    with _SEM:
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                cwd=tempfile.gettempdir(),  # no project CLAUDE.md / settings leak into the prompt
            )
        except subprocess.TimeoutExpired as exc:
            raise InferenceError(f"claude -p timed out after {_TIMEOUT_S:.0f}s") from exc
        except FileNotFoundError as exc:
            raise InferenceError("claude CLI not found") from exc
    if proc.returncode != 0:
        raise InferenceError(f"claude -p exited {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout or ""


class ClaudeCliAdapter(InferenceAdapter):
    name = "claude_cli"
    # Inference runs through YOUR Claude Code session (subscription), governed by you — treated as
    # local: the redaction tripwire for non-local API adapters does not apply.
    is_local = True

    def _extract(self, candidate: CandidateProfile, job: JobSpec) -> MatchExtraction:
        raw = _run_cli(_flatten(build_messages(candidate, job)))
        return parse_extraction(raw, candidate, job)
