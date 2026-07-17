"""Platform SQLite: ONE scoring-plane file + a boring numbered-migration runner.

`data/platform.db` (env `RM_PLATFORM_DB`, falling back to `RM_ACCOUNTS_DB` so existing deployments
keep their single persistent file) holds accounts AND the platform tables (postings, jobs, ...).
This is the SCORING PLANE ONLY — protected attributes / proxies never get a column here; they live
in the separate audit database (see `stores/data_planes.py`, boundary #2). A CI test greps every
column of this schema against PROTECTED_KEYS.

Migrations are numbered SQL files in `stores/migrations/NNN_*.sql`, tracked in `schema_version`
(each file + its version row applied as ONE transaction, so a crash can never leave a half-built
schema or strand rows in a copy-rename scratch table), plus a python column-upgrade pass (`_COLUMN_UPGRADES`) so a users table created by the older
AccountStore bootstrap gains the new columns in place. `migrate()` is idempotent and cheap when
current, so callers run it at startup/first-use rather than shipping a separate migrate command.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

_log = logging.getLogger("resume_matcher.stores.db")

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Serialize migration application within a process: stores construct concurrently at startup (the
# worker pool spins up threads that each construct stores, each calling migrate()). Idempotent DDL
# tolerates a race, but migration 003 rebuilds the consents table (non-idempotent — SQLite can't
# ALTER a CHECK), so one thread must finish applying before another checks the version.
_MIGRATE_LOCK = threading.Lock()

# Columns added to tables that may PRE-EXIST a migration (the AccountStore bootstrap creates a bare
# users table). SQLite backfills existing rows with the DEFAULT, so legacy users become 'student'.
# Phase-5 plain ADD COLUMNs also live here rather than in 004 (feasibility M1): a raw ALTER inside
# a migration script would wedge migrate() forever on any partial re-run; _ensure_columns is
# idempotent and runs on every migrate(), covering fresh, partially-migrated, and legacy DBs alike.
_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "users": {
        "role": "TEXT NOT NULL DEFAULT 'student' "
                "CHECK(role IN ('student','employer','coordinator','admin'))",
        "org_id": "INTEGER",
        "school_id": "INTEGER NOT NULL DEFAULT 1",
        # Phase 5 D4: alumni-ness is an ATTRIBUTE, never a role — no users rebuild, no grad_year.
        "alumni_status": "TEXT NOT NULL DEFAULT 'none' "
                         "CHECK(alumni_status IN ('none','self_claimed','verified'))",
    },
    "intro_requests": {
        # Phase 5 D5 (C2): 'bridged' iff the chosen path contains an alumni_bridge/mentorship edge.
        "origin": "TEXT NOT NULL DEFAULT 'organic' CHECK(origin IN ('organic','bridged'))",
    },
    "campus_events": {
        "checkin_code": "TEXT",  # Phase 5 C3: low-entropy second factor on top of registration
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


def _snapshot_before_migration(conn: sqlite3.Connection, db_path: str, current: int) -> None:
    """Snapshot the DB next to itself before the FIRST pending migration touches it.

    A migration is the one deploy step no rollback undoes: the cohost auto-deploy restores the
    previous image on a failed healthcheck, but the DB it hands that image back is already migrated.
    And because RM_ACCOUNTS_DB makes this file the live accounts store as well as the platform DB,
    "just the platform tables" is never the blast radius.

    Uses the online backup API, not a file copy: the app is serving and the DB is WAL, so `cp` can
    catch a torn page or miss the WAL. Best-effort by design — a demo that cannot write a snapshot
    (read-only mount, disk full) must still boot, so this logs and continues rather than blocking
    startup. The durable guard is the backup step in deploy/cohost/auto-deploy.sh, which refuses the
    deploy outright; this is the in-process net that also covers the very deploy that adds it.
    Set RM_MIGRATION_BACKUP=0 to skip.
    """
    if os.environ.get("RM_MIGRATION_BACKUP", "1").strip().lower() in ("0", "false", "no"):
        return
    # "Has anything worth keeping?" is a table question, not a file-size one: connect() has already
    # created the file and set WAL, so a brand-new DB is non-empty on disk. Ignore our own bookkeeping
    # table — a DB holding only schema_version has nothing to lose. Legacy DBs sit at version 0 with a
    # real bootstrapped `users` table, so version alone can't answer this either.
    tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                          "AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'").fetchone()[0]
    if not tables:
        return
    dest = f"{db_path}.pre-v{current}.bak"
    try:
        with closing(sqlite3.connect(dest)) as backup:
            conn.backup(backup)
        _log.warning("pre-migration snapshot written: %s (restore by copying it back over %s "
                     "after deleting the stale -wal/-shm)", dest, os.path.basename(db_path))
    except Exception as exc:                                  # noqa: BLE001 — must never block boot
        _log.error("pre-migration snapshot FAILED (%s): %s — continuing, but this migration is "
                   "not undoable; snapshot %s by hand if it holds anything you need", dest, exc,
                   db_path)


def migrate(path: str | None = None) -> int:
    """Apply pending migrations; returns how many were applied (0 == already current)."""
    p = path or platform_db_path()
    applied = 0
    with _MIGRATE_LOCK, closing(connect(p)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version("
            "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        pending = [f for f in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
                   if int(f.name[:3]) > current]
        if pending:
            _snapshot_before_migration(conn, p, current)
        for sql_file in pending:
            version = int(sql_file.name[:3])
            # ONE transaction per file, version row included: either the whole migration and its
            # schema_version row land, or neither does. Non-negotiable for 004's copy-rename
            # rebuilds — under executescript()'s per-statement autocommit an OOM-kill between
            # `DROP TABLE graph_edges` and the RENAME left the only copy of the rows in a scratch
            # table with schema_version still at 3. SQLite DDL is transactional, so a killed
            # process now rolls back to a clean pre-004 DB.
            conn.execute("BEGIN IMMEDIATE")
            try:
                _apply_script(conn, sql_file.read_text(encoding="utf-8"))
                # OR IGNORE: two threads may race the first migrate on a fresh DB; the DDL is all
                # IF NOT EXISTS so a double apply is harmless — the version row must not throw.
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(?,?)",
                    (version, time.time()),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            applied += 1
            _log.info("applied migration %s", sql_file.name)
        _ensure_columns(conn)
        _fold_in_legacy_accounts(conn, p)
        conn.commit()
    return applied


def _apply_script(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a migration file statement-by-statement on an OPEN transaction.

    `executescript()` cannot be used here: it COMMITs before running and then autocommits each
    statement, which is exactly what made a mid-rebuild crash unrecoverable. Statement boundaries
    come from sqlite3.complete_statement (SQLite's own parser — quotes/comments aware), not from a
    naive split(';'). Trailing comment-only text is not a statement and is ignored.
    """
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            conn.execute(buf)
            buf = ""
    if any(ln.strip() and not ln.strip().startswith("--") for ln in buf.splitlines()):
        raise sqlite3.ProgrammingError(f"migration ends with an unterminated statement: {buf[:80]!r}")


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
