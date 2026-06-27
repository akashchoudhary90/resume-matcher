"""Email + password accounts and saved projects — SQLite-backed, so they survive restarts/redeploys.

This is the persistence tier ("free forgets, paid remembers"): anonymous demo use stays ephemeral and
in-RAM (see api/demo.py); a SIGNED-IN user can SAVE a scored shortlist or fit-grid as a named project
and reopen it later. Saved projects therefore put the (already de-identified) score breakdown on disk —
consciously allowed (2026-06-26 decision). Raw résumé text is still never persisted: a project stores
exactly the session `to_dict()` the client already saw (redacted quotes only), nothing more.

Security posture — demo-grade, hardened where it is cheap to do so:
  * Passwords: PBKDF2-HMAC-SHA256, per-user 16-byte random salt, high iteration count (stdlib only).
  * Sessions: opaque 32-byte random tokens; only their SHA-256 is stored, delivered as an HttpOnly
    cookie, so a DB read can't reveal a usable token.
  * Every query is parameterized — no string-built SQL.
A new SQLite connection is opened per call (so the store is thread-safe under the API threadpool) and a
process lock serializes writers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing

from ..config import env_int

_PBKDF2_ITERS = 240_000


def _default_db_path() -> str:
    return os.environ.get("RM_ACCOUNTS_DB", os.path.join("data", "accounts.db"))


class AccountError(Exception):
    """A client-correctable problem (bad email, weak password, duplicate) -> HTTP 400."""


class AccountStore:
    """SQLite-backed users + auth tokens + saved projects."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or _default_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, closing(self._conn()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    pw_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens(
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    n_resumes INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL);
                """
            )
            conn.commit()

    # ---- passwords -----------------------------------------------------------------------------
    @staticmethod
    def _hash_pw(password: str, salt_hex: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERS
        ).hex()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    # ---- auth ----------------------------------------------------------------------------------
    def register(self, email: str, password: str) -> tuple[str, str]:
        email = (email or "").strip().lower()
        if "@" not in email or len(email) < 3:
            raise AccountError("Enter a valid email address.")
        if len(password or "") < 8:
            raise AccountError("Password must be at least 8 characters.")
        salt = secrets.token_hex(16)
        pw_hash = self._hash_pw(password, salt)
        with self._lock, closing(self._conn()) as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users(email, pw_hash, salt, created_at) VALUES(?,?,?,?)",
                    (email, pw_hash, salt, time.time()),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("That email is already registered — sign in instead.") from exc
            uid = cur.lastrowid
            conn.commit()
        return self._issue_token(uid), email

    def login(self, email: str, password: str) -> tuple[str, str]:
        email = (email or "").strip().lower()
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Always run the hash (even on unknown email) so timing doesn't reveal which emails exist.
        candidate = self._hash_pw(password or "", row["salt"] if row else "00" * 16)
        if row is None or not hmac.compare_digest(candidate, row["pw_hash"]):
            raise AccountError("Wrong email or password.")
        return self._issue_token(row["id"]), email

    def _issue_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock, closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO tokens(token_hash, user_id, created_at) VALUES(?,?,?)",
                (self._token_hash(token), user_id, time.time()),
            )
            conn.commit()
        return token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        th = self._token_hash(token)
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT u.id AS id, u.email AS email, t.created_at AS created_at FROM tokens t "
                "JOIN users u ON u.id = t.user_id WHERE t.token_hash=?",
                (th,),
            ).fetchone()
            if row is None:
                return None
            # Enforce token expiry SERVER-SIDE (not just via the cookie max-age): a leaked/stale token
            # stops working after the window, and we purge it lazily on use.
            if time.time() - row["created_at"] > cookie_max_age():
                conn.execute("DELETE FROM tokens WHERE token_hash=?", (th,))
                conn.commit()
                return None
        return {"id": row["id"], "email": row["email"]}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, closing(self._conn()) as conn:
            conn.execute("DELETE FROM tokens WHERE token_hash=?", (self._token_hash(token),))
            conn.commit()

    # ---- projects ------------------------------------------------------------------------------
    def save_project(self, user_id: int, name: str, mode: str, payload: dict) -> str:
        pid = secrets.token_urlsafe(12)
        n_resumes = int(payload.get("n_resumes", 0) or 0)
        with self._lock, closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO projects(id, user_id, name, mode, n_resumes, created_at, payload) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, user_id, (name or "Untitled").strip()[:120], mode, n_resumes,
                 time.time(), json.dumps(payload)),
            )
            conn.commit()
        return pid

    def list_projects(self, user_id: int) -> list[dict]:
        with self._lock, closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT id, name, mode, n_resumes, created_at FROM projects "
                "WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, user_id: int, pid: str) -> dict | None:
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id=? AND user_id=?", (pid, user_id)
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "name": row["name"], "mode": row["mode"],
                "created_at": row["created_at"], "payload": json.loads(row["payload"])}

    def delete_project(self, user_id: int, pid: str) -> bool:
        with self._lock, closing(self._conn()) as conn:
            cur = conn.execute("DELETE FROM projects WHERE id=? AND user_id=?", (pid, user_id))
            conn.commit()
            return cur.rowcount > 0


def cookie_max_age() -> int:
    """Session cookie lifetime in seconds (RM_ACCOUNTS_TOKEN_DAYS, default 30)."""
    return max(1, env_int("RM_ACCOUNTS_TOKEN_DAYS", 30)) * 86400
