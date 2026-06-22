"""Admin-password gate for the whole web app (HTTP Basic Auth).

Wired as an app-level dependency in app.py, so it covers the dashboard, every /api/* route, and the
docs. Behaviour:
  * RM_ADMIN_PASSWORD set   -> Basic auth required (browser shows a login prompt). User defaults to
                               "admin" (override with RM_ADMIN_USER). Constant-time comparison.
  * RM_ADMIN_PASSWORD unset -> open mode for LOCAL development only (a warning is logged once).

This protects a SYNTHETIC-DATA demo. It is NOT sufficient for real student PII — that needs York-side
governance + a hardened, institution-hosted deployment (see DEPLOY.md).
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_log = logging.getLogger("resume_matcher.auth")
_security = HTTPBasic(auto_error=False)
_warned = False

# Reachable WITHOUT credentials: the readiness probe the Docker HEALTHCHECK and the auto-deploy poller
# hit. It exposes no secrets and no PII (just status + counts), and must answer before login works.
_AUTH_EXEMPT_PATHS = {"/api/health"}

# Known-weak / placeholder passwords. A SET-but-weak password almost always means a real deployment
# shipped the .env.example default, so we fail fast rather than guard real PII with "admin".
_WEAK_PASSWORDS = {"admin", "password", "passwd", "changeme", "change_me_before_deploy", "secret", "123456"}


def assert_admin_password_strong() -> None:
    """Fail fast at startup if RM_ADMIN_PASSWORD is set to a known-weak/default value.

    Unset is allowed (open local-dev mode, handled per-request in require_auth with a warning); a weak
    *set* password is refused so the documented copy-the-example deploy path can't ship admin/admin.
    """
    password = os.environ.get("RM_ADMIN_PASSWORD")
    if password and password.strip().lower() in _WEAK_PASSWORDS:
        raise RuntimeError(
            f"RM_ADMIN_PASSWORD is set to a known-weak/default value ({password!r}). "
            "Set a strong password (e.g. in deploy/cohost/.env) before starting the app."
        )


def require_auth(
    request: Request, credentials: HTTPBasicCredentials | None = Depends(_security)
) -> None:
    global _warned
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return  # readiness probe — must work unauthenticated for healthchecks/deploy polling
    password = os.environ.get("RM_ADMIN_PASSWORD")
    if not password:
        if not _warned:
            _log.warning("RM_ADMIN_PASSWORD not set — running OPEN (local dev only). Do not deploy like this.")
            _warned = True
        return

    user = os.environ.get("RM_ADMIN_USER", "admin")
    ok = credentials is not None and secrets.compare_digest(
        credentials.username, user
    ) and secrets.compare_digest(credentials.password, password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
