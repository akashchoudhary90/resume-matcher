"""api/auth.py: the platform's per-user role gate + role-aware registration, and the Phase-5 A15/A16
admin-session layer (server-side sessions, password-rotation logout-all, the prod boot assertions)."""
from __future__ import annotations

import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import AccountError, get_account_store  # noqa: E402
from resume_matcher.api.auth import (  # noqa: E402
    _platform_exempt,
    _pw_fingerprint,
    assert_admin_password_strong,
    check_login,
    create_admin_session,
    destroy_admin_session,
    require_role,
    validate_admin_session,
)
from resume_matcher.stores.db import connect  # noqa: E402


def test_platform_exempt_scopes_jobs_prefix_to_the_poll_route(monkeypatch):
    """The `/api/jobs/` platform poll exemption must NOT un-gate the admin dashboard sub-route
    /api/jobs/{id}/shortlist (it has no per-route auth and relied on the admin gate)."""
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    assert _platform_exempt("/api/jobs/abc123") is True            # the poll route (own require_role)
    assert _platform_exempt("/api/jobs/abc123/shortlist") is False  # stays behind the admin gate
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "0")
    assert _platform_exempt("/api/jobs/abc123") is False           # platform off -> nothing exempt


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/coord", dependencies=[Depends(require_role("coordinator", "admin"))])
    def coord():
        return {"ok": True}

    @app.get("/me")
    def me(user: dict = Depends(require_role())):
        return user

    return app


def test_role_gate_401_403_200():
    store = get_account_store()
    student_tok, _ = store.register("s@york.ca", "password123")
    coord_tok, _ = store.create_user("c@york.ca", "password123", role="coordinator")

    client = TestClient(_app())
    assert client.get("/coord").status_code == 401          # not signed in

    client.cookies.set("rm_session", student_tok)
    assert client.get("/coord").status_code == 403          # wrong role
    me = client.get("/me")
    assert me.status_code == 200 and me.json()["role"] == "student"

    client.cookies.set("rm_session", coord_tok)
    assert client.get("/coord").status_code == 200          # right role
    assert client.get("/me").json()["role"] == "coordinator"


def test_employer_register_creates_pending_org_link():
    store = get_account_store()
    tok, _ = store.register("hr@acme.com", "password123", role="employer", org_name="Acme Corp")
    user = store.user_for_token(tok)
    assert user["role"] == "employer" and user["org_id"] is not None

    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(store.path)) as conn:
        conn.row_factory = sqlite3.Row
        link = conn.execute("SELECT status, school_id FROM employer_school_links WHERE org_id=?",
                            (user["org_id"],)).fetchone()
    assert link["status"] == "pending" and link["school_id"] == 1


def test_privileged_roles_cannot_self_register():
    store = get_account_store()
    with pytest.raises(AccountError):
        store.register("evil@x.com", "password123", role="coordinator")
    with pytest.raises(AccountError):
        store.register("evil@x.com", "password123", role="admin")


def test_second_employer_joins_same_org():
    store = get_account_store()
    t1, _ = store.register("a@acme.com", "password123", role="employer", org_name="Acme Corp")
    t2, _ = store.register("b@acme.com", "password123", role="employer", org_name="Acme Corp")
    u1, u2 = store.user_for_token(t1), store.user_for_token(t2)
    assert u1["org_id"] == u2["org_id"]


def test_user_dict_carries_alumni_status():
    # A16: C4's mentor surfaces + the _BROKER_VERIFY_LEVEL mapping read it off the user dict.
    store = get_account_store()
    tok, _ = store.register("alum@york.ca", "password123")
    assert store.user_for_token(tok)["alumni_status"] == "none"


# ---- A15 server-side admin sessions -------------------------------------------------------------
@pytest.fixture()
def admin(monkeypatch):
    monkeypatch.setenv("RM_ADMIN_USER", "admin")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "s3cret-and-long")
    return monkeypatch


def test_admin_session_is_random_hashed_at_rest_and_validates(admin):
    token = create_admin_session()
    assert validate_admin_session(token) is True
    with closing(connect()) as conn:
        rows = conn.execute("SELECT token_hash, pw_fingerprint FROM admin_sessions").fetchall()
    # the cleartext token is NEVER at rest — only its sha256 (same discipline as accounts tokens)
    assert len(rows) == 1 and token not in rows[0]["token_hash"]
    assert rows[0]["pw_fingerprint"] == _pw_fingerprint()


def test_logout_invalidates_server_side(admin):
    token = create_admin_session()
    destroy_admin_session(token)
    # the ROW is gone, so a captured copy of the cookie is dead too (the old stateless HMAC
    # cookie stayed valid forever — logout could only clear the browser's copy).
    assert validate_admin_session(token) is False
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_expired_admin_session_rejected_and_purged(admin):
    token = create_admin_session()
    with closing(connect()) as conn:
        conn.execute("UPDATE admin_sessions SET expires_at=?", (time.time() - 1,))
        conn.commit()
    assert validate_admin_session(token) is False
    with closing(connect()) as conn:                       # lazily purged on the way out
        assert conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_derivable_hmac_style_cookie_is_rejected(admin):
    """The pre-A15 cookie was hmac(f'{user}:{password}:rm-admin-session-v1', b'rm-admin') — anyone
    holding the admin password could compute it. It must now be just another unknown token."""
    import hashlib
    import hmac as _hmac

    key = b"admin:s3cret-and-long:rm-admin-session-v1"
    legacy = _hmac.new(key, b"rm-admin", hashlib.sha256).hexdigest()
    assert validate_admin_session(legacy) is False


def test_password_rotation_invalidates_all_outstanding_sessions(admin):
    # security M5: rotating the password IS the incident-response logout-all.
    a, b = create_admin_session(), create_admin_session()
    assert validate_admin_session(a) and validate_admin_session(b)
    admin.setenv("RM_ADMIN_PASSWORD", "a-different-strong-password")
    assert validate_admin_session(a) is False
    assert validate_admin_session(b) is False


def test_check_login_mints_a_session_only_on_correct_credentials(admin):
    assert check_login("admin", "wrong") is None
    assert check_login("nope", "s3cret-and-long") is None
    token = check_login("admin", "s3cret-and-long")
    assert token and validate_admin_session(token) is True


# ---- A16 prod boot assertions -------------------------------------------------------------------
def test_prod_refuses_unset_or_weak_password_and_insecure_cookie(monkeypatch):
    monkeypatch.setenv("RM_ENV", "prod")
    monkeypatch.delenv("RM_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="unset"):
        assert_admin_password_strong()

    monkeypatch.setenv("RM_ADMIN_PASSWORD", "admin")
    with pytest.raises(RuntimeError, match="weak"):
        assert_admin_password_strong()

    monkeypatch.setenv("RM_ADMIN_PASSWORD", "a-strong-password")
    monkeypatch.setenv("RM_COOKIE_SECURE", "0")
    with pytest.raises(RuntimeError, match="RM_COOKIE_SECURE"):
        assert_admin_password_strong()

    monkeypatch.setenv("RM_COOKIE_SECURE", "1")
    assert_admin_password_strong()                          # a correct prod env boots


def test_prod_tolerates_an_unset_cookie_flag(monkeypatch):
    # Only an EXPLICITLY off flag is fatal — an upgrade must not brick a running deployment.
    monkeypatch.setenv("RM_ENV", "prod")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "a-strong-password")
    monkeypatch.delenv("RM_COOKIE_SECURE", raising=False)
    assert_admin_password_strong()


def test_dev_still_boots_on_admin_admin(monkeypatch):
    # The synthetic-data demo posture is unchanged outside prod: warn, never refuse.
    monkeypatch.delenv("RM_ENV", raising=False)
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RM_COOKIE_SECURE", "0")
    assert_admin_password_strong()
