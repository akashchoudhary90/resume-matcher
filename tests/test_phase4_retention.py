"""Slice AK: retention purge + erasure cascade (true hard delete, no PII residue)."""
from __future__ import annotations

import time
from contextlib import closing

import pytest

from resume_matcher.stores.db import connect, migrate
from resume_matcher.stores.graph import NetworkStore
from resume_matcher.stores.relationships import RelationshipStore
from resume_matcher.stores.retention import run_retention


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("RM_ENV", "dev")
    monkeypatch.setenv("RM_GRAPH_PEPPER", "p")
    migrate()


def test_retention_purges_expired_edges_and_terminal_intros():
    now = time.time()
    with closing(connect()) as conn:
        # an expired edge and a live edge
        conn.execute("INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, "
                     "last_seen_at, provenance, created_at, updated_at, expires_at) "
                     "VALUES('e1',1,'k1',1,2,'linkedin_connection',?, 'self_upload',?,?,?)",
                     (now, now, now, now - 10))          # expired
        conn.execute("INSERT INTO graph_edges(id, school_id, edge_key, user_a, user_b, kind, "
                     "last_seen_at, provenance, created_at, updated_at, expires_at) "
                     "VALUES('e2',1,'k2',1,3,'interview',?, 'native',?,?,NULL)", (now, now, now))
        # a terminal intro past purge_after and a fresh one
        conn.execute("INSERT INTO intro_requests(id, school_id, posting_id, application_id, "
                     "requester_user_id, target_user_id, broker_user_id, hops, path_score, "
                     "path_json, status, created_at, expires_at, purge_after) "
                     "VALUES('i1',1,'p','a',1,3,2,2,0.5,'{}','declined',?,?,?)",
                     (now, now, now - 10))
        conn.commit()
    out = run_retention()
    assert out["edges_purged"] == 1 and out["intros_purged"] == 1
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0] == 1   # native kept
        assert conn.execute("SELECT COUNT(*) FROM intro_requests").fetchone()[0] == 0


def test_erasure_cascade_leaves_no_pii():
    rel = RelationshipStore()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO users(id,email,pw_hash,salt,created_at,role,school_id) "
                     "VALUES(7,'e@x.co','h','s',0,'student',1)")
        rel.upsert_edge(conn, 1, 7, 8, "interview", provenance="native")
        conn.commit()
    rel.create_vouch(school_id=1, voucher_user_id=8, subject_user_id=7, relationship="classmate",
                     evidence="great")
    NetworkStore().delete_my_network(7, reason="member_deleted")
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE user_a=7 OR user_b=7")\
            .fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vouches WHERE subject_user_id=7 OR "
                            "voucher_user_id=7").fetchone()[0] == 0
        # a permanent tombstone remains so nothing re-materializes
        assert conn.execute("SELECT reason FROM graph_suppressions WHERE user_id=7")\
            .fetchone()["reason"] == "member_deleted"
