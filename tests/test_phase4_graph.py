"""Slices AA + AB: the identity tokenizer (determinism, per-school divergence, fail-closed) and
the PSI-lite importer (intersects to consenting members, ZERO non-member residue, guards)."""
from __future__ import annotations

import time
from contextlib import closing

import pytest

from resume_matcher.stores import graph_tokens
from resume_matcher.stores.db import connect, migrate
from resume_matcher.stores.graph import GraphError, NetworkStore


@pytest.fixture(autouse=True)
def _dev_key(monkeypatch):
    monkeypatch.setenv("RM_ENV", "dev")
    monkeypatch.setenv("RM_GRAPH_PEPPER", "test-pepper-xyz")


# ---- Slice AA: tokenizer --------------------------------------------------------------------------
def test_canonical_identity_normalizes():
    assert graph_tokens.canonical_identity(first="José", last="García", company="Acme, Inc.") == \
        graph_tokens.canonical_identity(first="jose", last="garcia", company="acme")
    assert graph_tokens.canonical_identity(first="A") is None            # need first+last
    assert graph_tokens.canonical_identity(email="X@Example.COM").endswith("x@example.com")


def test_token_is_deterministic_and_per_school():
    t1, kv = graph_tokens.identity_token(1, first="Jane", last="Doe", company="Acme")
    t2, _ = graph_tokens.identity_token(1, first="Jane", last="Doe", company="Acme")
    t_other_school, _ = graph_tokens.identity_token(2, first="Jane", last="Doe", company="Acme")
    assert t1 == t2                       # stable
    assert t1 != t_other_school           # per-school divergence (cross-tenant unlinkable)
    assert kv == "dev1"


def test_tokenizer_fail_closed_without_key(monkeypatch):
    monkeypatch.delenv("RM_GRAPH_PEPPER", raising=False)
    monkeypatch.delenv("RM_GRAPH_KMS_KEY_ID", raising=False)
    assert graph_tokens.available() is False
    with pytest.raises(graph_tokens.TokenizerUnavailable):
        graph_tokens.identity_token(1, first="Jane", last="Doe")


# ---- Slice AB: importer ---------------------------------------------------------------------------
def _grant(conn, user_id, purpose):
    conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(?,?,?)",
                 (user_id, purpose, time.time()))


def _member(conn, user_id, email):
    conn.execute("INSERT INTO users(id, email, pw_hash, salt, created_at, role, school_id) "
                 "VALUES(?,?,'h','s',?, 'student', 1)", (user_id, email, time.time()))


def _csv(rows: list[tuple[str, str, str]]) -> bytes:
    body = "Notes:\n\nFirst Name,Last Name,Company,Position,Connected On\n"
    body += "\n".join(f"{f},{ln},{c},Engineer,01 Jan 2020" for f, ln, c in rows)
    return body.encode("utf-8")


def test_import_keeps_only_consenting_members_zero_residue(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    store = NetworkStore()
    with closing(connect()) as conn:
        _member(conn, 10, "uploader@york.ca")     # the uploader
        _member(conn, 20, "jane@york.ca")          # a discoverable member
        _member(conn, 30, "bob@york.ca")           # a member who did NOT opt in
        _grant(conn, 20, "graph_discoverable")
        conn.commit()
    # register discoverable identities (tokens only) for the two members
    store.register_identity(20, 1, first="Jane", last="Doe", company="Acme")
    store.register_identity(30, 1, first="Bob", last="Roe", company="Globex")  # bob has no consent

    # uploader's export: Jane (member+consent), Bob (member, no consent), + 5 pure non-members
    raw = _csv([("Jane", "Doe", "Acme"), ("Bob", "Roe", "Globex")]
               + [(f"Ext{i}", f"Person{i}", "Nowhere") for i in range(5)])
    result = store.import_csv(10, 1, raw)

    assert result["edges_created"] == 1           # only Jane (member + discoverable) becomes an edge
    with closing(connect()) as conn:
        edges = conn.execute("SELECT user_a, user_b, kind, consent_state FROM graph_edges").fetchall()
        assert len(edges) == 1
        assert set((edges[0]["user_a"], edges[0]["user_b"])) == {10, 20}
        assert edges[0]["kind"] == "linkedin_connection"
        assert edges[0]["consent_state"] == "pending"   # not shareable until owner opts the source in
        # ZERO non-member residue: no table holds Bob's or the 5 externals' identity/name
        assert conn.execute("SELECT COUNT(*) FROM member_graph_identity WHERE user_id=30")\
            .fetchone()[0] == 1   # Bob's own registration row exists (he registered), but...
        # ...no edge to Bob (no consent) and nothing at all for the 5 pure externals
        cols = [c[1] for t in ("graph_edges", "member_graph_identity")
                for c in conn.execute(f"PRAGMA table_info({t})")]
        assert "name" not in cols and "company" not in cols   # we never store contact names


def test_import_size_and_batch_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    store = NetworkStore()
    with pytest.raises(GraphError):
        store.import_csv(10, 1, _csv([("A", "B", "C")]))         # < MIN_IMPORT_ROWS (anti-oracle)
    with pytest.raises(GraphError):
        store.import_csv(10, 1, b"x" * (6 * 1024 * 1024))        # > 5 MB


def test_import_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    migrate()
    store = NetworkStore()
    monkeypatch.delenv("RM_GRAPH_PEPPER", raising=False)
    with pytest.raises(GraphError):
        store.import_csv(10, 1, _csv([("A", "B", "C"), ("D", "E", "F"), ("G", "H", "I")]))


def test_repudiation_tombstones_token(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    store = NetworkStore()
    store.repudiate(1, first="Jane", last="Doe", company="Acme")
    tok = graph_tokens.identity_token(1, first="Jane", last="Doe", company="Acme")[0]
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_suppressions WHERE identity_token=?",
                            (tok,)).fetchone()[0] == 1
