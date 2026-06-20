FROM python:3.12-slim

WORKDIR /app

# Core deps run the full app on the deterministic Mock backend (no model / GPU needed).
# Extras here power the web layer + the real-data demo: python-multipart (file uploads),
# pypdf + python-docx (parse uploaded .pdf/.docx resumes in memory).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    fastapi "uvicorn>=0.29" python-multipart pypdf python-docx

# OPTIONAL Claude backend (RM_DEMO_BACKEND=claude_cli): the Claude Code CLI native binary, used to
# score via your SUBSCRIPTION (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`) — no API key/bill.
# Same approach as the Kotak project. Gated behind a build ARG so the DEFAULT build fetches NO remote
# code (the installer runs as root). Enable: `--build-arg WITH_CLAUDE=1`. Pinned + non-fatal: a
# network blip never breaks a deploy (the adapter just falls back to the deterministic mock engine).
ARG WITH_CLAUDE=0
ARG CLAUDE_CLI_VERSION=2.1.132
ENV DISABLE_AUTOUPDATER=1
RUN if [ "$WITH_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
        && rm -rf /var/lib/apt/lists/* ; \
      ( curl -fsSL https://claude.ai/install.sh | HOME=/opt/claude-cli bash -s -- "$CLAUDE_CLI_VERSION" \
        && ln -s /opt/claude-cli/.local/bin/claude /usr/local/bin/claude \
        && claude --version ) \
      || echo "WARN: claude CLI install skipped — Claude backend unavailable, will use mock"; \
    fi

COPY resume_matcher ./resume_matcher
COPY scripts ./scripts

# Demo defaults: Mock backend, auto-load synthetic data, bind all interfaces.
# RM_ADMIN_PASSWORD MUST be supplied at runtime (the app runs OPEN if it is unset).
ENV RM_INFERENCE_BACKEND=mock \
    RM_AUTOLOAD=1 \
    RM_HOST=0.0.0.0 \
    RM_PORT=8000

EXPOSE 8000

# serve.py runs uvicorn with ws="none" (the app has no websockets; avoids a uvicorn/websockets clash).
# Single process on purpose: app state is in-memory, so one worker keeps it consistent.
CMD ["python", "scripts/serve.py"]
