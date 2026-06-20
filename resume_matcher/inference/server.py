"""stdio MCP server exposing a single `score_resume` tool (plan §C boundary).

Running scoring behind MCP keeps PII on-machine and lets the host (Claude Code) provide the model via
`sampling/createMessage`, so this server bundles no model SDK and the backend stays swappable. The
`evaluate_payload` core below is plain Python and is unit-testable without the mcp package installed.
"""
from __future__ import annotations

from typing import Any

from ..matching.evaluator import evaluate
from .adapter import InferenceAdapter, get_adapter
from .schema import CandidateProfile, JobSpec, ScoreResult


def evaluate_payload(candidate: dict[str, Any], job: dict[str, Any], adapter: InferenceAdapter | None = None) -> dict:
    """Core scoring entrypoint: dict in, dict out. The deterministic ranker (not the LLM) decides."""
    cand = CandidateProfile.model_validate(candidate)
    jspec = JobSpec.model_validate(job)
    result: ScoreResult = evaluate(cand, jspec, adapter or get_adapter())
    return result.model_dump()


def main() -> None:  # pragma: no cover - requires the mcp package + a host
    """Run the stdio MCP server. Requires `pip install -r requirements-extra.txt`."""
    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError as exc:
        raise SystemExit(
            "The 'mcp' package is required to run the MCP server. "
            "Install requirements-extra.txt, or run scripts/run_demo.py for an offline demo."
        ) from exc

    from .adapters.claude_code import ClaudeCodeAdapter, set_sampler

    server = FastMCP("resume-matcher")

    @server.tool()
    async def score_resume(candidate: dict, job: dict, ctx: Context) -> dict:
        """Score one candidate against one job. Returns a ScoreResult (fit/readiness, not hire %)."""

        def sampler(messages: list[dict]) -> str:
            # Bridge the adapter to MCP sampling so Claude Code runs the model on the host.
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            res = ctx.session.create_message(
                messages=[{"role": "user", "content": {"type": "text", "text": user}}],
                system_prompt=system,
                max_tokens=1500,
            )
            return res.content.text if hasattr(res.content, "text") else str(res.content)

        set_sampler(sampler)
        return evaluate_payload(candidate, job, adapter=ClaudeCodeAdapter())

    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
