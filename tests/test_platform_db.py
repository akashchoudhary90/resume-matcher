"""Platform DB migrations (stores/db.py + stores/migrations/): idempotency, in-place upgrade of a
legacy users table, the accounts.db fold-in, and the scoring-plane guarantee that no platform
column is a protected attribute or proxy (boundary #2 at schema level)."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from resume_matcher.stores import db as platform_db
from resume_matcher.stores.data_planes import PROTECTED_KEYS, ProtectedDataError, ScoringStore

# Entity-name columns (a school's or org's name is not a person's name — the PROTECTED_KEYS
# "name" proxy is about candidates). Anything else matching PROTECTED_KEYS fails the suite.
_ALLOWED_NAME_COLUMNS = {("schools", "name"), ("orgs", "name"), ("projects", "name")}


def _migrated(tmp_path) -> str:
    path = str(tmp_path / "platform.db")
    assert platform_db.migrate(path) >= 1
    return path


def test_fresh_migrate_creates_schema_and_seeds_york(tmp_path):
    path = _migrated(tmp_path)
    with closing(platform_db.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schools", "orgs", "employer_school_links", "users", "tokens", "postings",
                "posting_skills", "posting_events", "consents", "jobs", "match_results",
                "applications", "events"} <= tables
        assert conn.execute("SELECT name FROM schools WHERE id=1").fetchone()[0] == "York University"


def test_remigrate_is_idempotent(tmp_path):
    path = _migrated(tmp_path)
    assert platform_db.migrate(path) == 0  # nothing new to apply


def test_score_kind_check_is_schema_enforced(tmp_path):
    path = _migrated(tmp_path)
    with closing(platform_db.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO match_results(posting_id, student_id, fit_score, score_kind, "
                "result_json, computed_at) VALUES('p', 1, 50.0, 'hire_probability', '{}', 0)"
            )
        conn.execute(  # the honest kind is accepted (default matches the CHECK)
            "INSERT INTO match_results(posting_id, student_id, fit_score, result_json, computed_at)"
            " VALUES('p', 1, 50.0, '{}', 0)"
        )


def test_legacy_bare_users_table_upgraded_in_place(tmp_path):
    """A DB created by the pre-platform AccountStore bootstrap gains role/org_id/school_id, and
    existing rows are backfilled with the least-privileged role."""
    path = str(tmp_path / "accounts.db")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,"
            " pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO users(email, pw_hash, salt, created_at) VALUES('a@b.c','h','s',0)")
        conn.commit()
    platform_db.migrate(path)
    with closing(platform_db.connect(path)) as conn:
        row = conn.execute("SELECT role, school_id FROM users WHERE email='a@b.c'").fetchone()
        assert row["role"] == "student" and row["school_id"] == 1


def test_fold_in_legacy_accounts_db(tmp_path, monkeypatch):
    """An EMPTY platform DB adopts users/tokens/projects from a sibling data/accounts.db."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    with closing(sqlite3.connect(tmp_path / "data" / "accounts.db")) as legacy:
        legacy.execute(
            "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,"
            " pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        legacy.execute("CREATE TABLE tokens(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,"
                       " created_at REAL NOT NULL)")
        legacy.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,"
                       " name TEXT NOT NULL, mode TEXT NOT NULL, n_resumes INTEGER NOT NULL,"
                       " created_at REAL NOT NULL, payload TEXT NOT NULL)")
        legacy.execute("INSERT INTO users(email, pw_hash, salt, created_at)"
                       " VALUES('old@x.com','h','s',1.0)")
        legacy.execute("INSERT INTO tokens VALUES('th', 1, ?)", (time.time(),))
        legacy.execute("INSERT INTO projects VALUES('p1', 1, 'n', 'single', 2, 1.0, '{}')")
        legacy.commit()
    target = str(tmp_path / "platform.db")
    platform_db.migrate(target)
    with closing(platform_db.connect(target)) as conn:
        assert conn.execute("SELECT email FROM users").fetchone()["email"] == "old@x.com"
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        # folded-in legacy users get the least-privileged role
        assert conn.execute("SELECT role FROM users").fetchone()["role"] == "student"


def test_no_protected_columns_in_scoring_plane(tmp_path):
    """Boundary #2, mechanically: no platform table may carry a column named after a protected
    attribute or proxy. This is the CI gate PLATFORM.md requires for every future migration."""
    path = _migrated(tmp_path)
    with closing(platform_db.connect(path)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        offenders = []
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({table})"):
                name = col[1].lower()
                if name in PROTECTED_KEYS and (table, name) not in _ALLOWED_NAME_COLUMNS:
                    offenders.append(f"{table}.{name}")
        assert not offenders, f"protected attribute/proxy column(s) in scoring plane: {offenders}"


def test_assert_no_protected_blocks_alumni_status_and_grad_year_features():
    """Phase 5 (privacy F2): alumni_status and grad_year are legitimate platform columns but may
    never enter a scoring feature dict; graduation_year is a full protected proxy (no column
    exists, and the schema scan above trips on any future one)."""
    ScoringStore.assert_no_protected({"skills": 3, "years_experience": 2})  # benign features pass
    for bad in ({"alumni_status": "verified"}, {"grad_year": 2020}, {"graduation_year": 2020}):
        with pytest.raises(ProtectedDataError):
            ScoringStore.assert_no_protected(bad)
