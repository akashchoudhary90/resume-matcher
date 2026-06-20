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

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_log = logging.getLogger("resume_matcher.auth")
_security = HTTPBasic(auto_error=False)
_warned = False


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    global _warned
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
