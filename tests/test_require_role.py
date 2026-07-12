"""require_role (api/auth.py): the platform's per-user role gate, and role-aware registration."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import AccountError, get_account_store  # noqa: E402
from resume_matcher.api.auth import _platform_exempt, require_role  # noqa: E402


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
