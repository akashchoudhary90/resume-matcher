"""Phase-5 slice S1: migration 004 (graph_edges/applications rebuilds, new tables, A14 cleanup),
the _COLUMN_UPGRADES additions (users.alumni_status, intro_requests.origin,
campus_events.checkin_code), the NotificationStore (B4-lite), and the run_retention additions."""
from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing

import pytest

from resume_matcher import notify
from resume_matcher.stores import db as platform_db
from resume_matcher.stores.notifications import NotificationStore
from resume_matcher.stores.retention import run_retention

_REAL_MIGRATIONS = platform_db.MIGRATIONS_DIR
_PHASE5_TABLES = {"notifications", "mentor_profiles", "mentorship_offers", "affiliations",
                  "affiliation_claims", "event_checkins", "repudiation_requests",
                  "admin_sessions", "vouch_invites"}


def _v3_db(tmp_path, monkeypatch) -> str:
    """A DB stopped at schema_version 3, so 004 can be exercised against seeded Phase-4 rows."""
    mig = tmp_path / "migs-v3"
    mig.mkdir()
    for f in sorted(_REAL_MIGRATIONS.glob("00[1-3]_*.sql")):
        shutil.copy(str(f), str(mig / f.name))
    path = str(tmp_path / "platform.db")
    monkeypatch.setattr(platform_db, "MIGRATIONS_DIR", mig)
    assert platform_db.migrate(path) == 3
    monkeypatch.setattr(platform_db, "MIGRATIONS_DIR", _REAL_MIGRATIONS)
    return path


def _edge(conn, eid, a, b, kind, *, consent_state="pending", seen=100.0):
    conn.execute(
        "INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, weight, "
        "observation_count, last_seen_at, provenance, provenance_ref, consent_state, "
        "owner_user_id, created_at, updated_at, revoked_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, 1, f"{a}:{b}:{kind}:{eid}", a, b, kind, 1.0, 1, seen, "native", None,
         consent_state, None, seen, seen, None, None))


# ---- migration 004 ---------------------------------------------------------------------------------
def test_004_creates_phase5_tables_and_widens_checks(tmp_path):
    path = str(tmp_path / "platform.db")
    assert platform_db.migrate(path) >= 1
    assert platform_db.migrate(path) == 0  # twice-guard via schema_version
    with closing(platform_db.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert _PHASE5_TABLES <= tables
        # widened CHECKs: the new edge kinds/provenance and the 'withdrawn' application status
        for eid, kind in (("e1", "peer_coattendance"), ("e2", "classmate"),
                          ("e3", "org_comember"), ("e4", "mentorship")):
            _edge(conn, eid, 1, int(eid[1]) + 1, kind)
        conn.execute("UPDATE graph_edges SET provenance='affiliation' WHERE id='e2'")
        conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at, "
                     "updated_at) VALUES('a1','p1',1,'withdrawn',0,0)")
        with pytest.raises(sqlite3.IntegrityError):
            _edge(conn, "e9", 8, 9, "handshake")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at,"
                         " updated_at) VALUES('a2','p1',2,'bogus',0,0)")


def test_004_rebuild_preserves_rows_and_indexes_on_populated_db(tmp_path, monkeypatch):
    path = _v3_db(tmp_path, monkeypatch)
    with closing(platform_db.connect(path)) as conn:
        _edge(conn, "keep", 1, 2, "application", consent_state="shareable")
        conn.execute("INSERT INTO applications(id, posting_id, student_id, resume_id, status, "
                     "human_review_requested, note, created_at, updated_at) "
                     "VALUES('a1','p1',1,NULL,'applied',0,NULL,10.0,11.0)")
        conn.commit()
    assert platform_db.migrate(path) == 1  # 004 only
    with closing(platform_db.connect(path)) as conn:
        row = conn.execute("SELECT * FROM graph_edges WHERE id='keep'").fetchone()
        assert (row["consent_state"], row["user_b"], row["last_seen_at"]) == ("shareable", 2, 100.0)
        app = conn.execute("SELECT * FROM applications WHERE id='a1'").fetchone()
        assert (app["status"], app["updated_at"]) == ("applied", 11.0)
        edge_idx = {r[1]: r[2] for r in conn.execute("PRAGMA index_list(graph_edges)")}
        assert edge_idx.get("idx_gedges_key") == 1  # unique
        assert {"idx_gedges_a", "idx_gedges_b"} <= set(edge_idx)
        app_idx = {r[1] for r in conn.execute("PRAGMA index_list(applications)")}
        assert "idx_applications_posting" in app_idx
        with pytest.raises(sqlite3.IntegrityError):  # inline UNIQUE(posting_id, student_id) survived
            conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at,"
                         " updated_at) VALUES('a2','p1',1,'applied',0,0)")


def _crash_window(path: str, table: str) -> None:
    """Exactly what an OOM-kill between `DROP TABLE <t>` and `ALTER TABLE <t>_v2 RENAME TO <t>`
    leaves on disk: the original gone, its ONLY copy in the populated scratch, version back at 3."""
    with closing(platform_db.connect(path)) as conn:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
        conn.execute("DELETE FROM schema_version WHERE version=4")
        conn.commit()


def test_004_rerun_after_crash_between_drop_and_rename_recovers_rows(tmp_path):
    """The re-run must COMPLETE the half-done rebuild, never restart it destructively: dropping the
    scratch here would delete the last copy and wedge migrate() forever (feasibility M1)."""
    path = str(tmp_path / "platform.db")
    platform_db.migrate(path)
    with closing(platform_db.connect(path)) as conn:
        _edge(conn, "e1", 1, 2, "mentorship", consent_state="shareable")
        conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at, "
                     "updated_at) VALUES('a1','p1',1,'withdrawn',10.0,11.0)")
        conn.commit()
    for table in ("graph_edges", "applications"):
        _crash_window(path, table)
        assert platform_db.migrate(path) == 1  # completes, no OperationalError
        with closing(platform_db.connect(path)) as conn:
            row = conn.execute("SELECT * FROM graph_edges WHERE id='e1'").fetchone()
            assert (row["kind"], row["consent_state"]) == ("mentorship", "shareable")
            app = conn.execute("SELECT * FROM applications WHERE id='a1'").fetchone()
            assert (app["status"], app["updated_at"]) == ("withdrawn", 11.0)
            assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%\\_v2' "
                                "ESCAPE '\\'").fetchone() is None
            edge_idx = {r[1]: r[2] for r in conn.execute("PRAGMA index_list(graph_edges)")}
            assert edge_idx.get("idx_gedges_key") == 1
            assert {"idx_gedges_a", "idx_gedges_b"} <= set(edge_idx)
            assert "idx_applications_posting" in \
                {r[1] for r in conn.execute("PRAGMA index_list(applications)")}
    assert platform_db.migrate(path) == 0  # and the version row lands exactly once


def test_migration_script_is_one_transaction(tmp_path, monkeypatch):
    """executescript() autocommits statement-by-statement, which is what turns a mid-rebuild crash
    into data loss. A failing script must leave NOTHING behind — not even its first statement."""
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "001_x.sql").write_text("CREATE TABLE half_applied(id INTEGER);\n"
                                   "SELECT nonexistent_fn();\n", encoding="utf-8")
    monkeypatch.setattr(platform_db, "MIGRATIONS_DIR", mig)
    path = str(tmp_path / "platform.db")
    with pytest.raises(sqlite3.Error):
        platform_db.migrate(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='half_applied'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0


def test_a14_cleanup_revokes_only_non_accepted_interview_edges(tmp_path, monkeypatch):
    path = _v3_db(tmp_path, monkeypatch)
    now = time.time()
    with closing(platform_db.connect(path)) as conn:
        conn.execute("INSERT INTO applications(id, posting_id, student_id, created_at, updated_at)"
                     " VALUES('a1','p1',1,?,?)", (now, now))
        conn.execute("INSERT INTO applications(id, posting_id, student_id, created_at, updated_at)"
                     " VALUES('a2','p2',3,?,?)", (now, now))
        conn.execute("INSERT INTO interview_slots(id, application_id, proposed_by, starts_at, "
                     "ends_at, status, created_at) VALUES('s1','a1',2,0,1,'accepted',?)", (now,))
        conn.execute("INSERT INTO interview_slots(id, application_id, proposed_by, starts_at, "
                     "ends_at, status, created_at) VALUES('s2','a2',4,0,1,'declined',?)", (now,))
        _edge(conn, "ok", 1, 2, "interview")    # student 1 <-> proposer 2: accepted slot exists
        _edge(conn, "bad", 3, 4, "interview")   # student 3 <-> proposer 4: declined only
        conn.commit()
    assert platform_db.migrate(path) == 1
    with closing(platform_db.connect(path)) as conn:
        assert conn.execute("SELECT consent_state FROM graph_edges WHERE id='ok'").fetchone()[0] \
            == "pending"
        bad = conn.execute("SELECT consent_state, revoked_at FROM graph_edges WHERE id='bad'"
                           ).fetchone()
        assert bad["consent_state"] == "revoked" and bad["revoked_at"] is not None
        revoked_at = bad["revoked_at"]
        conn.execute("DELETE FROM schema_version WHERE version=4")  # idempotency: force a re-run
        conn.commit()
    assert platform_db.migrate(path) == 1
    with closing(platform_db.connect(path)) as conn:
        assert conn.execute("SELECT consent_state FROM graph_edges WHERE id='ok'").fetchone()[0] \
            == "pending"
        assert conn.execute("SELECT revoked_at FROM graph_edges WHERE id='bad'").fetchone()[0] \
            == revoked_at


# ---- _COLUMN_UPGRADES additions ---------------------------------------------------------------------
def test_column_upgrades_alumni_origin_checkin_code(tmp_path):
    path = str(tmp_path / "platform.db")
    platform_db.migrate(path)
    with closing(platform_db.connect(path)) as conn:
        conn.execute("INSERT INTO users(email, pw_hash, salt, created_at) VALUES('a@b.c','h','s',0)")
        assert conn.execute("SELECT alumni_status FROM users").fetchone()[0] == "none"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE users SET alumni_status='ancient'")
        assert "checkin_code" in {r[1] for r in conn.execute("PRAGMA table_info(campus_events)")}
        conn.execute("INSERT INTO intro_requests(id, school_id, posting_id, application_id, "
                     "requester_user_id, target_user_id, broker_user_id, hops, path_score, "
                     "path_json, created_at, expires_at) VALUES('i1',1,'p','a',1,2,3,2,0.5,'[]',0,1)")
        assert conn.execute("SELECT origin FROM intro_requests WHERE id='i1'").fetchone()[0] \
            == "organic"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE intro_requests SET origin='teleport' WHERE id='i1'")


def test_legacy_bare_users_table_gains_alumni_status(tmp_path):
    """A pre-platform AccountStore DB upgrades in place; legacy rows backfill to 'none'."""
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
        assert conn.execute("SELECT alumni_status FROM users").fetchone()["alumni_status"] == "none"


# ---- NotificationStore (B4-lite) ---------------------------------------------------------------------
def test_notification_feed_and_mark_read_are_user_scoped(tmp_path):
    store = NotificationStore(str(tmp_path / "platform.db"))
    nid = store.notify(1, 1, "message", "New message", "You have a new message on a thread.")
    other = store.notify(2, 1, "vouch_received", "Someone vouched for you")
    feed = store.feed(1)
    assert [i["id"] for i in feed["items"]] == [nid] and feed["unread"] == 1
    assert store.mark_read(1, [other]) == 0        # security L1: can't touch user 2's rows
    assert store.feed(2)["unread"] == 1
    assert store.mark_read(1) == 1                 # None = all of MY unread
    assert store.feed(1)["unread"] == 0
    assert store.feed(1, unread_only=True)["items"] == []


def test_notification_email_fanout_kinds_and_email_invariant(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda to, subject, body: sent.append((to, subject)))
    store = NotificationStore(str(tmp_path / "platform.db"))
    store.notify(1, 1, "posting_approved", "Your posting is live", "Approved by career services.",
                 email_to="hr@acme.com")
    assert sent == [("hr@acme.com", "Your posting is live")]
    store.notify(1, 1, "message", "No email side channel")
    assert len(sent) == 1
    with pytest.raises(ValueError):
        store.notify(1, 1, "carrier_pigeon", "t")
    # erasure hygiene (FL-L4): composed titles/bodies may never embed a user email address
    with pytest.raises(ValueError):
        store.notify(1, 1, "message", "Message from stu@york.ca")
    with pytest.raises(ValueError):
        store.notify(1, 1, "message", "New message", "Reply to stu@york.ca today")
    assert store.feed(1)["unread"] == 2            # the rejected calls stored nothing


def test_notification_purge_windows(tmp_path):
    path = str(tmp_path / "platform.db")
    store = NotificationStore(path)
    now = time.time()
    old_read = store.notify(1, 1, "message", "old read")
    old_unread = store.notify(1, 1, "message", "old unread")
    fresh_read = store.notify(1, 1, "message", "fresh read")
    aging_unread = store.notify(1, 1, "message", "aging unread")
    store.mark_read(1, [old_read, fresh_read])
    with closing(platform_db.connect(path)) as conn:
        conn.execute("UPDATE notifications SET created_at=? WHERE id=?",
                     (now - 91 * 86400, old_read))       # read + past 90d -> purged
        conn.execute("UPDATE notifications SET created_at=? WHERE id=?",
                     (now - 181 * 86400, old_unread))    # unread + past 180d -> purged
        conn.execute("UPDATE notifications SET created_at=? WHERE id=?",
                     (now - 91 * 86400, aging_unread))   # unread at 91d: inside 180d -> stays
        conn.commit()
    assert store.purge() == 2
    with closing(platform_db.connect(path)) as conn:
        left = {r[0] for r in conn.execute("SELECT id FROM notifications")}
    assert left == {fresh_read, aging_unread}


# ---- run_retention additions (§2.15) -----------------------------------------------------------------
def test_run_retention_scrubs_and_purges_phase5_rows(tmp_path):
    path = str(tmp_path / "platform.db")
    platform_db.migrate(path)
    now = time.time()
    with closing(platform_db.connect(path)) as conn:
        # undecided name_review past its 30d TTL -> expired + PII scrubbed (privacy F6)
        conn.execute("INSERT INTO repudiation_requests(id, school_id, kind, first, last, company,"
                     " status, created_at, expires_at) VALUES('r1',1,'name_review','First','Last',"
                     "'Acme','pending',?,?)", (now - 40 * 86400, now - 10 * 86400))
        # unconfirmed email challenge past 48h -> expired + hashes scrubbed
        conn.execute("INSERT INTO repudiation_requests(id, school_id, kind, email_hash, "
                     "challenge_hash, status, created_at, expires_at) VALUES('r2',1,"
                     "'email_challenge','eh','ch','pending',?,?)", (now - 3 * 86400, now - 86400))
        # decided row past purge_after -> hard-deleted
        conn.execute("INSERT INTO repudiation_requests(id, school_id, kind, status, created_at, "
                     "expires_at, purge_after) VALUES('r3',1,'name_review','denied',?,?,?)",
                     (now - 90 * 86400, now - 60 * 86400, now - 86400))
        # pending and not yet expired -> untouched
        conn.execute("INSERT INTO repudiation_requests(id, school_id, kind, first, last, status, "
                     "created_at, expires_at) VALUES('r4',1,'name_review','F','L','pending',?,?)",
                     (now, now + 86400))
        conn.execute("INSERT INTO admin_sessions(token_hash, pw_fingerprint, created_at, "
                     "expires_at) VALUES('dead','fp',?,?)", (now - 86400, now - 1))
        conn.execute("INSERT INTO admin_sessions(token_hash, pw_fingerprint, created_at, "
                     "expires_at) VALUES('live','fp',?,?)", (now, now + 3600))
        conn.commit()
    out = run_retention(path)
    assert out["repudiations_scrubbed"] == 2 and out["repudiations_purged"] == 1
    assert out["admin_sessions_purged"] == 1 and out["notifications_purged"] == 0
    assert {"intros_expired", "edges_purged", "intros_purged"} <= set(out)  # legacy keys intact
    with closing(platform_db.connect(path)) as conn:
        r1 = conn.execute("SELECT * FROM repudiation_requests WHERE id='r1'").fetchone()
        assert r1["status"] == "expired" and r1["purge_after"] is not None
        assert r1["first"] is None and r1["last"] is None and r1["company"] is None
        r2 = conn.execute("SELECT * FROM repudiation_requests WHERE id='r2'").fetchone()
        assert r2["status"] == "expired" and r2["email_hash"] is None \
            and r2["challenge_hash"] is None
        assert conn.execute("SELECT COUNT(*) FROM repudiation_requests WHERE id='r3'"
                            ).fetchone()[0] == 0
        r4 = conn.execute("SELECT * FROM repudiation_requests WHERE id='r4'").fetchone()
        assert r4["status"] == "pending" and r4["first"] == "F"
        assert {r[0] for r in conn.execute("SELECT token_hash FROM admin_sessions")} == {"live"}
