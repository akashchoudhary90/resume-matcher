"""Platform SQLite: ONE scoring-plane file + a boring numbered-migration runner.

`data/platform.db` (env `RM_PLATFORM_DB`, falling back to `RM_ACCOUNTS_DB` so existing deployments
keep their single persistent file) holds accounts AND the platform tables (postings, jobs, ...).
This is the SCORING PLANE ONLY — protected attributes / proxies never get a column here; they live
in the separate audit database (see `stores/data_planes.py`, boundary #2). A CI test greps every
column of this schema against PROTECTED_KEYS.

Migrations are numbered SQL files in `stores/migrations/NNN_*.sql`, tracked in `schema_version`,
plus a python column-upgrade pass (`_COLUMN_UPGRADES`) so a users table created by the older
AccountStore bootstrap gains the new columns in place. `migrate()` is idempotent and cheap when
current, so callers run it at startup/first-use rather than shipping a separate migrate command.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

_log = logging.getLogger("resume_matcher.stores.db")

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Columns added to tables that may PRE-EXIST a migration (the AccountStore bootstrap creates a bare
# users table). SQLite backfills existing rows with the DEFAULT, so legacy users become 'student'.
_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "users": {
        "role": "TEXT NOT NULL DEFAULT 'student' "
                "CHECK(role IN ('student','employer','coordinator','admin'))",
        "org_id": "INTEGER",
        "school_id": "INTEGER NOT NULL DEFAULT 1",
    },
}


def platform_db_path() -> str:
    """The platform's SQLite file. RM_PLATFORM_DB wins; RM_ACCOUNTS_DB keeps existing deployments
    (and the per-test tmp isolation fixture) on one file; default is data/platform.db."""
    return (
        os.environ.get("RM_PLATFORM_DB")
        or os.environ.get("RM_ACCOUNTS_DB")
        or os.path.join("data", "platform.db")
    )


def connect(path: str | None = None) -> sqlite3.Connection:
    """New connection (per call — thread-safe under the API threadpool), WAL, busy timeout."""
    p = path or platform_db_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(path: str | None = None) -> int:
    """Apply pending migrations; returns how many were applied (0 == already current)."""
    p = path or platform_db_path()
    applied = 0
    with closing(connect(p)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version("
            "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        for sql_file in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(sql_file.name[:3])
            if version <= current:
                continue
            conn.executescript(sql_file.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES(?,?)",
                (version, time.time()),
            )
            applied += 1
            _log.info("applied migration %s", sql_file.name)
        _ensure_columns(conn)
        _fold_in_legacy_accounts(conn, p)
        conn.commit()
    return applied


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """ALTER-in any missing upgrade column on tables that may pre-date the migration."""
    for table, cols in _COLUMN_UPGRADES.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            continue  # table doesn't exist at all — the migration SQL owns creating it
        have = {row[1] for row in info}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _fold_in_legacy_accounts(conn: sqlite3.Connection, path: str) -> None:
    """One-time copy of users/tokens/projects from a legacy data/accounts.db into an EMPTY platform
    DB (the PLATFORM.md 'accounts.db folds in as migration 001' story). No-op when the platform DB
    already has users or IS the legacy file."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    legacy = os.path.join("data", "accounts.db")
    if not os.path.exists(legacy) or os.path.abspath(legacy) == os.path.abspath(path):
        return
    src = sqlite3.connect(legacy)
    src.row_factory = sqlite3.Row
    try:
        for row in src.execute("SELECT id, email, pw_hash, salt, created_at FROM users"):
            conn.execute(
                "INSERT OR IGNORE INTO users(id, email, pw_hash, salt, created_at) "
                "VALUES(?,?,?,?,?)",
                (row["id"], row["email"], row["pw_hash"], row["salt"], row["created_at"]),
            )
        for row in src.execute("SELECT token_hash, user_id, created_at FROM tokens"):
            conn.execute(
                "INSERT OR IGNORE INTO tokens(token_hash, user_id, created_at) VALUES(?,?,?)",
                (row["token_hash"], row["user_id"], row["created_at"]),
            )
        for row in src.execute("SELECT id, user_id, name, mode, n_resumes, created_at, payload "
                               "FROM projects"):
            conn.execute(
                "INSERT OR IGNORE INTO projects(id, user_id, name, mode, n_resumes, created_at, "
                "payload) VALUES(?,?,?,?,?,?,?)",
                tuple(row),
            )
        _log.info("folded legacy accounts.db into %s", path)
    except sqlite3.Error:  # legacy file unreadable/partial — never block startup on the copy
        _log.warning("legacy accounts.db fold-in failed; continuing with empty users", exc_info=True)
    finally:
        src.close()
