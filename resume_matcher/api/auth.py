"""Admin gate for the whole web app — FORM-based sign-in backed by a SERVER-SIDE session.

Wired as an app-level dependency in app.py, so it covers the dashboard, the demo, every /api/* route,
and the docs. Behaviour:
  * RM_ADMIN_PASSWORD set   -> a valid session cookie is required. Visitors land on a SIGN-IN PAGE
                               (/login) and POST username/password (default admin/admin via
                               RM_ADMIN_USER/RM_ADMIN_PASSWORD) to /api/login, which sets an HttpOnly
                               cookie. No browser Basic-Auth popup; unauthenticated page loads are
                               redirected to /login, unauthenticated API calls get 401.
  * RM_ADMIN_PASSWORD unset -> open mode for LOCAL development only (a warning is logged once).

Phase 5 A15 replaced the old STATELESS cookie (an HMAC derived from the admin secret) with real
server-side sessions in `admin_sessions`, because the stateless design bought restart-survival at
three costs: the cookie value was derivable from the password, `/api/logout` could only clear the
browser's copy (a captured cookie stayed valid forever), and expiry was cookie-side only. Now:
  * the cookie is a 32-byte random token; only its sha256 is stored, so a DB read yields nothing
    usable (same at-rest discipline as api/accounts.py tokens);
  * expiry (RM_ADMIN_SESSION_HOURS, default 12) is enforced server-side and swept by run_retention;
  * logout DELETEs the row — the token is dead everywhere, not just in that browser;
  * every row carries the pw_fingerprint it was minted under, so rotating RM_ADMIN_PASSWORD (the
    incident-response move) instantly invalidates every outstanding session (security M5).
A16: under RM_ENV=prod the app REFUSES to boot on an unset/weak password or an explicitly insecure
cookie; every other environment keeps the warn-only synthetic-data demo posture.

The per-user platform accounts (api/accounts.py, cookie `rm_session`, `require_role` below) are a
separate, additive layer used inside — see docs/PLATFORM.md.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from contextlib import closing

from fastapi import HTTPException, Request, status

from ..config import env_int, env_str

_log = logging.getLogger("resume_matcher.auth")
_warned = False

ADMIN_COOKIE = "rm_admin"

# Reachable WITHOUT a session: the readiness probe, the sign-in page + its endpoints, and the PUBLIC
# Defense-File verifier (a regulator/auditor/opposing counsel has no admin login; the verifier exposes
# no secrets or PII — it only re-checks a file the caller already holds).
_AUTH_EXEMPT_PATHS = {
    "/api/health", "/login", "/api/login", "/api/logout",
    "/verify", "/api/verify", "/api/defense-file/pubkey", "/manifest.webmanifest",
}

# When the platform is enabled, its routes carry their OWN per-user auth (require_role over the
# rm_session account cookie) — employers/students/coordinators have accounts, not the shared admin
# password. The admin gate is thereby demoted to the ops/legacy surface (docs/PLATFORM.md). The
# flag is read per-request so RM_PLATFORM_ENABLED=0 deployments keep today's posture untouched.
_PLATFORM_PREFIXES = (
    # "/api/account" is deliberately WITHOUT its trailing slash: DELETE /api/account (the Phase-5
    # self-serve erasure) is an exact path, and the old "/api/account/" entry would leave it behind
    # the admin gate, 401-ing before require_role ever runs (feasibility M2).
    "/api/postings", "/api/coordinator/", "/api/skills", "/api/account",
    "/api/students/", "/api/applications/", "/api/events", "/api/messages/",
    "/api/interview-slots/", "/api/schools", "/api/orgs/", "/employer", "/coordinator",
    "/student",
    # Phase 4 — relationship graph / warm intros (each route declares its own require_role;
    # /api/graph/repudiate is intentionally public for non-member data-subject requests).
    "/api/network", "/api/graph", "/api/intros", "/api/vouches",
    # Phase 5 — the api/phase5.py router (docs/PHASE5.md §3.2), plus the PUBLIC /repudiate page
    # (B3): a non-member data subject has no account and must never meet the admin sign-in wall.
    "/api/notifications", "/api/mentorship", "/api/affiliations", "/api/alumni", "/repudiate",
)


def _platform_exempt(path: str) -> bool:
    from ..config import env_flag

    if not env_flag("RM_PLATFORM_ENABLED", False):
        return False
    # `/api/jobs/` carries the platform poll route /api/jobs/{id} (its own require_role), but the
    # prefix must NOT swallow the admin-gated dashboard sub-route /api/jobs/{id}/shortlist: exempt
    # ONLY a single trailing segment (the poll id), never a deeper path.
    if path.startswith("/api/jobs/"):
        return "/" not in path[len("/api/jobs/"):]
    return path.startswith(_PLATFORM_PREFIXES)

# Known-weak / placeholder passwords. admin/admin is INTENTIONALLY allowed for the synthetic-data demo
# (the user wants it) — outside RM_ENV=prod we warn but never refuse to start.
_WEAK_PASSWORDS = {"admin", "password", "passwd", "changeme", "change_me_before_deploy", "secret", "123456"}


def _admin_secret() -> tuple[str, str] | None:
    """(user, password) when an admin password is configured, else None (= open local-dev mode)."""
    password = os.environ.get("RM_ADMIN_PASSWORD")
    if not password:
        return None
    return os.environ.get("RM_ADMIN_USER", "admin"), password


def _is_prod() -> bool:
    return env_str("RM_ENV", "").strip().lower() in ("prod", "production")


def assert_admin_password_strong() -> None:
    """A16. RM_ENV=prod: REFUSE to boot on an unset/weak admin password, or on a cookie explicitly
    marked insecure. Anywhere else: warn only — the demo is intentionally usable with admin/admin.

    Only an EXPLICITLY off RM_COOKIE_SECURE is fatal: leaving it unset keeps the pre-A16 default so
    an upgrade cannot brick a running deployment (deploy/cohost's compose already sets it to 1)."""
    from ..config import env_flag

    password = os.environ.get("RM_ADMIN_PASSWORD")
    weak = bool(password) and password.strip().lower() in _WEAK_PASSWORDS
    cookie_off = bool(env_str("RM_COOKIE_SECURE", "").strip()) and not env_flag("RM_COOKIE_SECURE", False)

    if _is_prod():
        problems = []
        if not password:
            problems.append("RM_ADMIN_PASSWORD is unset (the app would run OPEN to the internet)")
        elif weak:
            problems.append("RM_ADMIN_PASSWORD is a known-weak/default value")
        if cookie_off:
            problems.append("RM_COOKIE_SECURE is off (session cookies would ride plain HTTP)")
        if problems:
            raise RuntimeError(
                "RM_ENV=prod refuses to start: " + "; ".join(problems)
                + ". Fix the deployment env (see deploy/cohost/.env.example) or unset RM_ENV."
            )
        return

    if weak:
        _log.warning(
            "RM_ADMIN_PASSWORD is a weak/default value (%r) — fine for a synthetic-data demo, but set a "
            "strong password if this guards anything sensitive. RM_ENV=prod refuses this outright.",
            password,
        )
    if cookie_off:
        _log.warning("RM_COOKIE_SECURE is explicitly off — session cookies will ride plain HTTP. "
                     "RM_ENV=prod refuses this outright.")


# ---- server-side admin sessions (A15) ------------------------------------------------------------
# migrate() is idempotent but not free (it re-reads schema_version + PRAGMAs three tables), and
# require_auth runs on EVERY gated request — so remember which DB files we have already migrated.
_MIGRATED: set[str] = set()
_MIGRATED_LOCK = threading.Lock()


def _admin_conn():
    from ..stores.db import connect, migrate, platform_db_path

    path = platform_db_path()
    with _MIGRATED_LOCK:
        if path not in _MIGRATED:
            migrate(path)
            _MIGRATED.add(path)
    return connect(path)


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _pw_fingerprint() -> str:
    """Fingerprint of the CURRENT admin credential. Stored on every session row; a mismatch at
    validate time means the credential was rotated, which is the logout-all signal (security M5)."""
    user, password = _admin_secret() or ("", "")
    return hashlib.sha256(f"rm-admin:{user}:{password}".encode("utf-8")).hexdigest()[:16]


def admin_session_max_age() -> int:
    """Session lifetime in seconds (RM_ADMIN_SESSION_HOURS, default 12). One authority for BOTH the
    server-side expires_at and the cookie's max-age, so they can never drift apart."""
    return max(1, env_int("RM_ADMIN_SESSION_HOURS", 12)) * 3600


def create_admin_session() -> str:
    """Mint a session and return the CLEARTEXT token for the cookie (only its sha256 is stored)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with closing(_admin_conn()) as conn:
        conn.execute(
            "INSERT INTO admin_sessions(token_hash, pw_fingerprint, created_at, expires_at) "
            "VALUES(?,?,?,?)",
            (_token_hash(token), _pw_fingerprint(), now, now + admin_session_max_age()),
        )
        conn.commit()
    return token


def validate_admin_session(cookie: str) -> bool:
    """True iff the cookie names a live session minted under the CURRENT password. An expired or
    stale-fingerprint row is purged on the way out, so a rotated-out token stops working AND stops
    taking up space (run_retention sweeps the ones nobody ever presents again)."""
    if not cookie:
        return False
    th = _token_hash(cookie)
    with closing(_admin_conn()) as conn:
        row = conn.execute(
            "SELECT pw_fingerprint, expires_at FROM admin_sessions WHERE token_hash=?", (th,)
        ).fetchone()
        if row is None:
            return False
        if row["expires_at"] < time.time() or not hmac.compare_digest(
            row["pw_fingerprint"], _pw_fingerprint()
        ):
            conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (th,))
            conn.commit()
            return False
    return True


def destroy_admin_session(cookie: str) -> None:
    """The real logout: the row goes, so a captured copy of the cookie dies with it."""
    if not cookie:
        return
    with closing(_admin_conn()) as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_token_hash(cookie),))
        conn.commit()


def check_login(username: str, password: str) -> str | None:
    """Validate submitted credentials; return the cookie value to set on success, else None."""
    secret = _admin_secret()
    if secret is None:
        return None
    user, pw = secret
    ok = hmac.compare_digest(username or "", user) and hmac.compare_digest(password or "", pw)
    return create_admin_session() if ok else None


SESSION_COOKIE = "rm_session"  # the per-user account cookie (api/accounts.py layer)


def require_role(*roles: str):
    """FastAPI dependency factory for the platform's per-user role gate (docs/PLATFORM.md).

    No signed-in user -> 401; signed in but role not in `roles` -> 403. With no roles given it just
    requires a signed-in user. Returns the user dict {id, email, role, org_id, school_id,
    alumni_status} so routes can take it as a parameter. This is the PER-USER layer — independent
    of the shared admin gate."""

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
    if _admin_secret() is None:
        if not _warned:
            _log.warning("RM_ADMIN_PASSWORD not set — running OPEN (local dev only). Do not deploy like this.")
            _warned = True
        return  # open mode: never touch the DB on the hot path
    if validate_admin_session(request.cookies.get(ADMIN_COOKIE, "")):
        return
    # Not signed in: redirect a browser to the sign-in page; return 401 to API/XHR callers.
    if request.url.path.startswith("/api"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
    raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
