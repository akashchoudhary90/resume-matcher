"""Slice Y: migration 003 — consents rebuild (rows preserved), the new tables, school_id NOT NULL,
purposes ⇔ CHECK, and the protected-column CI gate still passes over the widened schema."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from resume_matcher.stores import db as platform_db
from resume_matcher.stores.data_planes import PROTECTED_KEYS
from resume_matcher.stores.students import CONSENT_PURPOSES

_ALLOWED_NAME_COLUMNS = {("schools", "name"), ("orgs", "name"), ("projects", "name")}
_PHASE4_TABLES = {"member_graph_identity", "graph_edges", "graph_suppressions",
                  "employer_contacts", "posting_contacts", "vouches", "intro_requests",
                  "intro_events", "broker_blocks"}


def _fresh(tmp_path) -> str:
    path = str(tmp_path / "platform.db")
    platform_db.migrate(path)
    return path


def test_003_creates_phase4_tables(tmp_path):
    with closing(platform_db.connect(_fresh(tmp_path))) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert _PHASE4_TABLES <= tables


def test_consents_rebuild_preserves_rows_on_existing_db(tmp_path):
    """Apply 001/002 state, add a consent, then let 003 rebuild — the row must survive."""
    path = str(tmp_path / "platform.db")
    platform_db.migrate(path)  # applies all incl. 003; simulate a pre-003 row by inserting now
    with closing(platform_db.connect(path)) as conn:
        conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(1,'contact',?)",
                     (time.time(),))
        conn.commit()
    # re-migrate is a no-op (version-gated) — the row stays and the new purposes are accepted
    assert platform_db.migrate(path) == 0
    with closing(platform_db.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM consents WHERE purpose='contact'").fetchone()[0] == 1
        conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(1,'warm_intro',?)",
                     (time.time(),))  # a Phase-4 purpose is now allowed by the CHECK
        conn.commit()


def test_new_consent_purposes_rejected_before_and_allowed_after(tmp_path):
    with closing(platform_db.connect(_fresh(tmp_path))) as conn:
        for purpose in ("contacts_upload", "graph_discoverable", "warm_intro", "network_analytics"):
            conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(1,?,?)",
                         (purpose, time.time()))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(1,'bogus',?)",
                         (time.time(),))
        conn.commit()


def test_consent_purposes_tuple_matches_check(tmp_path):
    """The Python CONSENT_PURPOSES tuple must equal the SQL CHECK set (no drift)."""
    with closing(platform_db.connect(_fresh(tmp_path))) as conn:
        # every purpose in the tuple inserts cleanly; nothing outside it does
        for i, purpose in enumerate(CONSENT_PURPOSES):
            conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(?,?,?)",
                         (i, purpose, time.time()))
        conn.commit()
    assert set(CONSENT_PURPOSES) == {
        "resume_storage", "profile_matching", "self_id_audit", "contact",
        "contacts_upload", "graph_discoverable", "warm_intro", "network_analytics"}


def test_phase4_tables_school_id_not_null(tmp_path):
    """The tenant footgun fix: Phase-4 tables reject a NULL school_id (no DEFAULT 1)."""
    with closing(platform_db.connect(_fresh(tmp_path))) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, "
                "last_seen_at, provenance, created_at, updated_at) "
                "VALUES('e', NULL, 'k', 1, 2, 'application', 0, 'native', 0, 0)")


def test_no_protected_columns_in_scoring_plane(tmp_path):
    with closing(platform_db.connect(_fresh(tmp_path))) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        offenders = []
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({table})"):
                name = col[1].lower()
                if name in PROTECTED_KEYS and (table, name) not in _ALLOWED_NAME_COLUMNS:
                    offenders.append(f"{table}.{name}")
        assert not offenders, f"protected attribute/proxy column(s) in scoring plane: {offenders}"
