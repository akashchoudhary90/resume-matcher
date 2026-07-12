"""Admin gate for the whole web app — FORM-based sign-in backed by a session cookie.

Wired as an app-level dependency in app.py, so it covers the dashboard, the demo, every /api/* route,
and the docs. Behaviour:
  * RM_ADMIN_PASSWORD set   -> a valid session cookie is required. Visitors land on a SIGN-IN PAGE
                               (/login) and POST username/password (default admin/admin via
                               RM_ADMIN_USER/RM_ADMIN_PASSWORD) to /api/login, which sets an HttpOnly
                               cookie. No browser Basic-Auth popup; unauthenticated page loads are
                               redirected to /login, unauthenticated API calls get 401.
  * RM_ADMIN_PASSWORD unset -> open mode for LOCAL development only (a warning is logged once).

The session cookie is STATELESS: its value is an HMAC derived from the admin secret, so it survives
restarts with no server-side store and is exactly as strong as RM_ADMIN_PASSWORD is kept. It is
constant-time compared. The per-user demo accounts (api/accounts.py, cookie `rm_session`) are a
separate, additive layer used to save projects once inside.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import HTTPException, Request, status

_log = logging.getLogger("resume_matcher.auth")
_warned = False

ADMIN_COOKIE = "rm_admin"

# Reachable WITHOUT a session: the readiness probe, the sign-in page + its endpoints, and the PUBLIC
# Defense-File verifier (a regulator/auditor/opposing counsel has no admin login; the verifier exposes
# no secrets or PII — it only re-checks a file the caller already holds).
_AUTH_EXEMPT_PATHS = {
    "/api/health", "/login", "/api/login", "/api/logout",
    "/verify", "/api/verify", "/api/defense-file/pubkey",
}

# When the platform is enabled, its routes carry their OWN per-user auth (require_role over the
# rm_session account cookie) — employers/students/coordinators have accounts, not the shared admin
# password. The admin gate is thereby demoted to the ops/legacy surface (docs/PLATFORM.md). The
# flag is read per-request so RM_PLATFORM_ENABLED=0 deployments keep today's posture untouched.
_PLATFORM_PREFIXES = (
    "/api/postings", "/api/jobs/", "/api/coordinator/", "/api/skills", "/api/account/",
    "/api/students/", "/api/applications/", "/employer", "/coordinator", "/student",
)


def _platform_exempt(path: str) -> bool:
    from ..config import env_flag

    return env_flag("RM_PLATFORM_ENABLED", False) and path.startswith(_PLATFORM_PREFIXES)

# Known-weak / placeholder passwords. admin/admin is INTENTIONALLY allowed for the synthetic-data demo
# (the user wants it) — we warn but never refuse to start.
_WEAK_PASSWORDS = {"admin", "password", "passwd", "changeme", "change_me_before_deploy", "secret", "123456"}


def _admin_secret() -> tuple[str, str] | None:
    """(user, password) when an admin password is configured, else None (= open local-dev mode)."""
    password = os.environ.get("RM_ADMIN_PASSWORD")
    if not password:
        return None
    return os.environ.get("RM_ADMIN_USER", "admin"), password


def assert_admin_password_strong() -> None:
    """Warn (do NOT refuse) on a weak admin password.

    The demo is intentionally usable with admin/admin, so a weak password only logs a warning now —
    use a strong RM_ADMIN_PASSWORD if the gate ever guards anything sensitive (the dashboard is
    synthetic data; real applicant data lives behind the per-user demo accounts)."""
    password = os.environ.get("RM_ADMIN_PASSWORD")
    if password and password.strip().lower() in _WEAK_PASSWORDS:
        _log.warning(
            "RM_ADMIN_PASSWORD is a weak/default value (%r) — fine for a synthetic-data demo, but set a "
            "strong password if this guards anything sensitive.", password
        )


def session_token() -> str | None:
    """Expected value of the admin session cookie: a stateless HMAC of the admin secret (None if open)."""
    secret = _admin_secret()
    if secret is None:
        return None
    user, password = secret
    key = f"{user}:{password}:rm-admin-session-v1".encode("utf-8")
    return hmac.new(key, b"rm-admin", hashlib.sha256).hexdigest()


def check_login(username: str, password: str) -> str | None:
    """Validate submitted credentials; return the cookie value to set on success, else None."""
    secret = _admin_secret()
    if secret is None:
        return None
    user, pw = secret
    ok = hmac.compare_digest(username or "", user) and hmac.compare_digest(password or "", pw)
    return session_token() if ok else None


SESSION_COOKIE = "rm_session"  # the per-user account cookie (api/accounts.py layer)


def require_role(*roles: str):
    """FastAPI dependency factory for the platform's per-user role gate (docs/PLATFORM.md).

    No signed-in user -> 401; signed in but role not in `roles` -> 403. With no roles given it just
    requires a signed-in user. Returns the user dict {id, email, role, org_id, school_id} so routes
    can take it as a parameter. This is the PER-USER layer — independent of the shared admin gate."""

    def _dep(request: Request) -> dict:
        from .accounts import get_account_store  # deferred: accounts imports stores.db at import time

        user = get_account_store().user_for_token(request.cookies.get(SESSION_COOKIE))
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
        if roles and user.get("role") not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This action needs a {' or '.join(roles)} account.",
            )
        return user

    return _dep


def require_auth(request: Request) -> None:
    global _warned
    if request.url.path in _AUTH_EXEMPT_PATHS or _platform_exempt(request.url.path):
        return  # readiness probe, sign-in endpoints, and per-user-authed platform routes
    expected = session_token()
    if expected is None:
        if not _warned:
            _log.warning("RM_ADMIN_PASSWORD not set — running OPEN (local dev only). Do not deploy like this.")
            _warned = True
        return
    cookie = request.cookies.get(ADMIN_COOKIE, "")
    if cookie and hmac.compare_digest(cookie, expected):
        return
    # Not signed in: redirect a browser to the sign-in page; return 401 to API/XHR callers.
    if request.url.path.startswith("/api"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
    raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
