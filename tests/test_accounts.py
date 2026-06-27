"""Email+password accounts + saved projects (persistence tier). SQLite store + HTTP surface."""
import io
import sqlite3
import time

import pytest

from resume_matcher.api.accounts import AccountError, AccountStore


def test_register_login_and_password_is_hashed(tmp_path):
    db = str(tmp_path / "a.db")
    store = AccountStore(db)
    token, email = store.register("Jane@Example.com ", "supersecret")
    assert email == "jane@example.com"                       # normalized
    assert store.user_for_token(token)["email"] == "jane@example.com"
    # the password is hashed + salted, never stored in the clear
    row = sqlite3.connect(db).execute("SELECT pw_hash, salt FROM users").fetchone()
    assert "supersecret" not in row[0] and len(row[1]) == 32
    # login issues a working token; the wrong password is rejected
    t2, _ = store.login("jane@example.com", "supersecret")
    assert store.user_for_token(t2)["email"] == "jane@example.com"
    with pytest.raises(AccountError):
        store.login("jane@example.com", "wrong-password")


def test_duplicate_email_and_weak_inputs_rejected(tmp_path):
    store = AccountStore(str(tmp_path / "a.db"))
    store.register("a@b.com", "password1")
    with pytest.raises(AccountError):
        store.register("a@b.com", "password2")            # duplicate
    with pytest.raises(AccountError):
        store.register("not-an-email", "password1")       # bad email
    with pytest.raises(AccountError):
        store.register("c@b.com", "short")                # weak password


def test_logout_invalidates_token(tmp_path):
    store = AccountStore(str(tmp_path / "a.db"))
    token, _ = store.register("a@b.com", "password1")
    assert store.user_for_token(token) is not None
    store.logout(token)
    assert store.user_for_token(token) is None


def test_token_expires_server_side(tmp_path):
    # A token older than the window must stop working server-side (not just via the cookie max-age).
    db = str(tmp_path / "a.db")
    store = AccountStore(db)
    token, _ = store.register("a@b.com", "password1")
    assert store.user_for_token(token) is not None
    with sqlite3.connect(db) as conn:                       # age it 40 days past the 30-day default
        conn.execute("UPDATE tokens SET created_at=?", (time.time() - 40 * 86400,))
        conn.commit()
    assert store.user_for_token(token) is None              # expired -> rejected and purged
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0


def test_projects_are_isolated_per_user(tmp_path):
    store = AccountStore(str(tmp_path / "a.db"))
    t1, _ = store.register("u1@b.com", "password1")
    t2, _ = store.register("u2@b.com", "password1")
    u1, u2 = store.user_for_token(t1), store.user_for_token(t2)
    pid = store.save_project(u1["id"], "My shortlist", "single", {"n_resumes": 3, "results": []})
    assert len(store.list_projects(u1["id"])) == 1
    assert store.list_projects(u2["id"]) == []                # other user can't see it
    assert store.get_project(u2["id"], pid) is None           # nor open it
    got = store.get_project(u1["id"], pid)
    assert got["name"] == "My shortlist" and got["payload"]["n_resumes"] == 3
    assert store.delete_project(u2["id"], pid) is False       # can't delete another user's project
    assert store.delete_project(u1["id"], pid) is True
    assert store.get_project(u1["id"], pid) is None


def _api_client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    return TestClient(create_app())


def test_account_api_save_and_reopen_flow():
    client = _api_client()
    assert client.get("/api/account/me").json()["user"] is None
    assert client.get("/api/projects").status_code == 401          # gated before login

    r = client.post("/api/account/register", json={"email": "r@b.com", "password": "password1"})
    assert r.status_code == 200
    assert client.get("/api/account/me").json()["user"]["email"] == "r@b.com"

    files = [("resumes", ("alice.txt", io.BytesIO(b"Python and SQL developer. " * 4), "text/plain"))]
    run = client.post("/api/demo/run", data={"required_skills": "python;sql", "job_text": "Python with SQL."},
                      files=files)
    sid = run.json()["session_id"]
    save = client.post(f"/api/demo/session/{sid}/save", json={"name": "Round 1"})
    assert save.status_code == 200
    pid = save.json()["id"]

    projects = client.get("/api/projects").json()
    assert len(projects) == 1 and projects[0]["name"] == "Round 1" and projects[0]["mode"] == "single"
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["payload"]["session_id"] == sid                    # the saved snapshot reopens

    client.post("/api/account/logout")
    assert client.get("/api/projects").status_code == 401          # logout revokes access


def test_save_requires_login():
    client = _api_client()
    files = [("resumes", ("a.txt", io.BytesIO(b"Python SQL developer. " * 4), "text/plain"))]
    sid = client.post("/api/demo/run", data={"required_skills": "python"}, files=files).json()["session_id"]
    assert client.post(f"/api/demo/session/{sid}/save", json={"name": "x"}).status_code == 401


def test_login_is_rate_limited(monkeypatch):
    # Password brute force on /api/account/login is throttled after the burst (security-review fix).
    monkeypatch.setenv("RM_AUTH_RATE_BURST", "3")
    monkeypatch.setenv("RM_AUTH_RATE_PER_MIN", "1")     # ~0.017/s refill — negligible during the test
    client = _api_client()
    codes = [client.post("/api/account/login", json={"email": "x@y.com", "password": "guess"}).status_code
             for _ in range(6)]
    assert 429 in codes
