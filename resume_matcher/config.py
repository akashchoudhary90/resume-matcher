"""Central inventory + typed access for the project's RM_* environment configuration.

Previously these knobs were read inline (os.environ.get / ad-hoc int parsing) across api/demo.py and
api/app.py. This module is the ONE documented place for them. Two access styles, on purpose:

  * `env_int` / `env_flag` / `env_str` — read at CALL time, so a running server or a test can change a
    value (used for the request-time knobs like the rate limits and the kill switch).
  * `DemoConfig.from_env()` — a frozen snapshot of the demo's STARTUP limits, taken once at import
    (matching the prior module-constant behavior in api/demo.py).

NB: the inference-backend knobs (RM_CLAUDE_CLI_MODEL / _TIMEOUT / _FILE_TIMEOUT / _CONCURRENCY,
RM_INFERENCE_BACKEND, RM_OLLAMA_HOST) are read in inference/adapters/* close to where they are used;
they are listed here for discoverability but not re-homed, to keep the adapter boundary self-contained.

The request-time demo DoS/gate knobs (RM_DEMO_RATE_BURST, RM_DEMO_RATE_PER_MIN,
RM_DEMO_MAX_CONCURRENT_RUNS, and the "full functionality, limited quantity" usage gate
RM_DEMO_FREE_RUNS / RM_DEMO_QUOTA_WINDOW_MIN) are read in api/app.py via env_int at construction time,
next to where they are enforced. RM_DEMO_FREE_RUNS=0 (default) disables the quota; the public demo
sets it in deploy/cohost/docker-compose.cohost.yml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUTHY = ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` on unset/blank/invalid."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var ('1'/'true'/'yes'/'on' == True)."""
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in _TRUTHY


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class DemoConfig:
    """Ephemeral-demo startup limits (snapshot at import; see module docstring)."""

    max_resumes: int
    max_file_mb: int
    ttl_minutes: int
    max_sessions: int
    backend: str
    concurrency: int
    send_file: bool
    cache_max: int
    max_jobs: int
    batch_roles: bool
    vision_min_text: int

    @classmethod
    def from_env(cls) -> "DemoConfig":
        return cls(
            max_resumes=env_int("RM_DEMO_MAX_RESUMES", 10),
            max_file_mb=env_int("RM_DEMO_MAX_FILE_MB", 4),
            ttl_minutes=env_int("RM_DEMO_TTL_MINUTES", 30),
            max_sessions=env_int("RM_DEMO_MAX_SESSIONS", 100),
            backend=env_str("RM_DEMO_BACKEND", "claude_cli"),
            concurrency=max(1, env_int("RM_DEMO_CONCURRENCY", 4)),
            send_file=env_flag("RM_DEMO_SEND_FILE", True),
            cache_max=env_int("RM_DEMO_CACHE_MAX", 512),
            max_jobs=max(1, env_int("RM_DEMO_MAX_JOBS", 3)),  # roles in the multi-job fit grid
            # Grid fast path: ONE LLM call per resume covering ALL roles (instead of one per
            # resume×role cell). Kill switch for A/B'ing extraction quality on real traffic.
            batch_roles=env_flag("RM_DEMO_BATCH_ROLES", True),
            # Vision (file-direct) only pays off when the model can see something the text layer
            # doesn't: use it when the locally-extracted text is shorter than this many chars
            # (scans/images); text-layer PDFs take the faster, more reliable text path.
            vision_min_text=env_int("RM_DEMO_VISION_MIN_TEXT", 200),
        )
