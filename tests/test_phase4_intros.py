"""Slices AC/AD/AE/AF: native edge builder, consent-gated pathfinder, double-opt-in intro flow
(incl. the two adversarial CRITICALS: no self-accept, IDOR blocked), and vouches.

Phase-5 slice S2 adds the graph-store integrity fixes to the same surfaces: the A13 pre-consent
guard (end-to-end through the build_edges job — FH-H1), the A14 accepted-only interview fold, the
A2 broker-consent prune + its in-transaction binding check (SM-M2), and the C3 check-in folds.
Slice S5 adds the ROUTE-level half: the intro routes actually passing `broker_ok` (a store that can
prune is worthless if no caller asks it to), the sentinel-0 hiring manager, C2's origin, and the
evidence-card exposure log.
"""
from __future__ import annotations

import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402
from resume_matcher.stores.intros import (  # noqa: E402
    IntroError,
    IntroStore,
    edge_score,
    find_paths,
    path_origin,
    rank_path,
)
from resume_matcher.stores.relationships import RelationshipStore  # noqa: E402


@pytest.fixture(autouse=True)
def _dev_key(monkeypatch):
    monkeypatch.setenv("RM_ENV", "dev")
    monkeypatch.setenv("RM_GRAPH_PEPPER", "test-pepper")


# ---- Slice AD: pure ranking (no DB) ---------------------------------------------------------------
def test_edge_score_recency_decays_but_never_zeroes():
    now = 1_000_000_000.0
    fresh = edge_score("interview", now, now)
    stale = edge_score("interview", now - 400 * 86400, now)
    assert fresh > stale > 0


def test_rank_path_product_penalizes_length():
    now = 1_000_000_000.0
    one_strong = rank_path([("interview", now)], now)
    two_hops = rank_path([("interview", now), ("message_thread", now)], now)
    assert one_strong > two_hops       # each extra hop multiplies in a <1 factor


# ---- graph fixture: a school with a broker path -------------------------------------------------
@pytest.fixture()
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    monkeypatch.setenv("RM_INFERENCE_BACKEND", "mock")
    accounts = get_account_store()
    tok = {}
    tok["student"], _ = accounts.register("stu@york.ca", "password123")
    tok["broker"], _ = accounts.register("alum@york.ca", "password123")   # a well-connected peer
    tok["employer"], _ = accounts.register("hr@acme.com", "password123",
                                           role="employer", org_name="Acme Corp")
    tok["coord"], _ = accounts.create_user("c@york.ca", "password123", role="coordinator")
    ids = {k: accounts.user_for_token(v)["id"] for k, v in tok.items()}
    return TestClient(create_app()), tok, ids, accounts


def _as(client, tok, who):
    client.cookies.set("rm_session", tok[who])
    return client


def _grant_graph(client, tok, who):
    _as(client, tok, who)
    for p in ("graph_discoverable", "warm_intro"):
        client.post("/api/graph/consents", json={"purpose": p, "granted": True})


def _wait_jobs_done(timeout: float = 20.0):
    """Wait until the worker pool drains the queue (jobs are enqueued by routes under test)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(connect()) as conn:
            busy = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if not busy:
            return
        time.sleep(0.05)
    raise AssertionError("jobs did not drain")


def _seed_broker_path(ids):
    """student -- (verified_vouch) --> broker -- (interview) --> employer(hiring mgr), all shareable
    and both-endpoints-discoverable (granted in the test)."""
    rel = RelationshipStore()
    with closing(connect()) as conn:
        rel.upsert_edge(conn, 1, ids["student"], ids["broker"], "verified_vouch",
                        provenance="vouch", consent_state="shareable")
        rel.upsert_edge(conn, 1, ids["broker"], ids["employer"], "interview",
                        provenance="native", consent_state="shareable")
        conn.commit()


def test_pathfinder_requires_both_endpoint_consent(platform):
    client, tok, ids, _ = platform
    _seed_broker_path(ids)
    rel = RelationshipStore()
    # nobody discoverable yet -> no consented path
    assert find_paths(rel, ids["student"], ids["employer"], 1) == []
    # grant all three -> the path lights up
    for who in ("student", "broker", "employer"):
        _grant_graph(client, tok, who)
    paths = find_paths(rel, ids["student"], ids["employer"], 1)
    assert paths and paths[0]["broker"] == ids["broker"] and paths[0]["hops"] == 2


def test_available_is_boolean_and_gated_behind_application(platform):
    client, tok, ids, _ = platform
    _seed_broker_path(ids)
    for who in ("student", "broker", "employer"):
        _grant_graph(client, tok, who)
    # make a live posting by the employer (the hiring manager = created_by)
    _as(client, tok, "coord")
    org_id = get_account_store().user_for_token(tok["employer"])["org_id"]
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tok, "employer")
    pid = client.post("/api/postings", json={"fields": {"title": "Dev", "description": "Python."},
                      "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tok, "coord")
    client.post(f"/api/coordinator/postings/{pid}/approve")

    _as(client, tok, "student")
    # before applying: gated to False even though a path exists
    assert client.get(f"/api/intros/available/{pid}").json() == {"warm_intro_available": False}
    # apply, then it flips True and stays a bare boolean (no hops/via_mutuals leaked)
    for p in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": p, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", b"Alex. Python and SQL. " * 6, "text/plain")})
    client.post(f"/api/postings/{pid}/apply")
    body = client.get(f"/api/intros/available/{pid}").json()
    assert body == {"warm_intro_available": True}


def _live_posting_with_application(client, tok, ids):
    _seed_broker_path(ids)
    for who in ("student", "broker", "employer"):
        _grant_graph(client, tok, who)
    _as(client, tok, "coord")
    org_id = get_account_store().user_for_token(tok["employer"])["org_id"]
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tok, "employer")
    pid = client.post("/api/postings", json={"fields": {"title": "Dev", "description": "Python."},
                      "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tok, "coord")
    client.post(f"/api/coordinator/postings/{pid}/approve")
    _as(client, tok, "student")
    for p in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": p, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", b"Alex. Python and SQL. " * 6, "text/plain")})
    app_id = client.post(f"/api/postings/{pid}/apply").json()["application_id"]
    return pid, app_id


def test_double_opt_in_intro_and_no_self_accept(platform):
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)

    _as(client, tok, "student")
    r = client.post("/api/intros/requests", json={"application_id": app_id})
    assert r.status_code == 201
    intro_id = r.json()["intro_id"]

    # CRITICAL: the requesting student cannot accept their OWN request (broker-only)
    assert client.post(f"/api/intros/requests/{intro_id}/accept",
                       json={"relationship": "worked_together"}).status_code == 403
    # student sees it pending, broker not revealed yet
    mine = client.get("/api/intros/requests/mine").json()["requests"]
    assert mine[0]["status"] == "requested" and mine[0]["broker_user_id"] is None

    # the broker sees it in their inbox and accepts, writing a vouch
    _as(client, tok, "broker")
    inbox = client.get("/api/intros/inbox").json()["requests"]
    assert any(x["id"] == intro_id for x in inbox)
    assert client.post(f"/api/intros/requests/{intro_id}/accept",
                       json={"relationship": "worked_together",
                             "evidence": "Worked with them on a data project."}).json()["status"] \
        == "accepted"

    # now the student sees the broker revealed
    _as(client, tok, "student")
    assert client.get("/api/intros/requests/mine").json()["requests"][0]["broker_user_id"] \
        == ids["broker"]

    # the employer's evidence card shows the intro + vouch, but it is NOT in match_results
    _as(client, tok, "employer")
    card = client.get(f"/api/intros/for-application/{app_id}").json()
    assert card["claim_kind"] == "job_related_evidence_not_hire_recommendation"
    assert card["warm_intros"] and card["vouches"]


def test_intro_idor_blocked(platform):
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    # the broker (a different user) tries to create an intro on the student's application
    _as(client, tok, "broker")
    assert client.post("/api/intros/requests", json={"application_id": app_id}).status_code == 404


def test_decline_is_silent(platform):
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    _as(client, tok, "student")
    intro_id = client.post("/api/intros/requests", json={"application_id": app_id}).json()["intro_id"]
    _as(client, tok, "broker")
    assert client.post(f"/api/intros/requests/{intro_id}/decline").json() == {"ok": True}
    # requester only ever sees a neutral status; no "declined-by" identity is exposed
    _as(client, tok, "student")
    mine = client.get("/api/intros/requests/mine").json()["requests"][0]
    assert mine["status"] == "declined" and mine["broker_user_id"] is None
    # intro_events carry no free-text PII (status transitions + opaque ids only)
    with closing(connect()) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(intro_events)")]
    assert "note" not in cols and "body" not in cols


# ---- Slice AF: vouches ----------------------------------------------------------------------------
def test_self_vouch_low_weight_subject_can_contest(platform):
    client, tok, ids, _ = platform
    _as(client, tok, "broker")
    v = client.post("/api/vouches", json={"subject_user_id": ids["student"],
                    "relationship": "classmate", "evidence": "Reach me at a@b.com or 416-555-0100"}).json()
    assert v["edge_kind"] == "self_vouch"   # self-authored -> floor weight, no verified edge
    # evidence was contact-PII-redacted at ingest
    with closing(connect()) as conn:
        ev = conn.execute("SELECT evidence_redacted FROM vouches WHERE id=?",
                          (v["vouch_id"],)).fetchone()["evidence_redacted"]
    assert "a@b.com" not in ev and "416-555" not in ev

    # the subject can see and contest it; contesting drops the edge from traversal
    _as(client, tok, "student")
    about = client.get("/api/vouches/about-me").json()["vouches"]
    assert about and about[0]["id"] == v["vouch_id"]
    assert client.post(f"/api/vouches/{v['vouch_id']}/contest",
                       json={"note": "I don't know this person"}).json()["status"] == "contested"
    with closing(connect()) as conn:
        state = conn.execute("SELECT consent_state FROM graph_edges WHERE provenance_ref=?",
                            (v["vouch_id"],)).fetchone()
    assert state["consent_state"] == "revoked"


def test_coordinator_verify_upgrades_vouch(platform):
    client, tok, ids, _ = platform
    _as(client, tok, "broker")
    v = client.post("/api/vouches", json={"subject_user_id": ids["student"],
                    "relationship": "worked_together"}).json()
    _as(client, tok, "coord")
    assert client.post(f"/api/vouches/{v['vouch_id']}/verify",
                       json={"verify_level": "coordinator"}).json()["verify_level"] == "coordinator"
    with closing(connect()) as conn:
        kinds = {r["kind"]: r["consent_state"] for r in
                 conn.execute("SELECT kind, consent_state FROM graph_edges WHERE provenance_ref=?",
                              (v["vouch_id"],))}
    assert kinds.get("verified_vouch") == "pending"      # upgraded edge exists
    assert kinds.get("self_vouch") == "revoked"          # old low-weight edge retired


# ---- Phase 5 / A13: consent is not retroactive ---------------------------------------------------
def _live_posting(client, tok):
    _as(client, tok, "coord")
    org_id = get_account_store().user_for_token(tok["employer"])["org_id"]
    client.post(f"/api/coordinator/org-links/{org_id}/approve")
    _as(client, tok, "employer")
    pid = client.post("/api/postings", json={"fields": {"title": "Dev", "description": "Python."},
                      "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    client.post(f"/api/postings/{pid}/submit")
    _as(client, tok, "coord")
    client.post(f"/api/coordinator/postings/{pid}/approve")
    return pid


def test_backfill_ignores_preconsent_interactions(platform):
    """FH-H1 (hard requirement), end-to-end. The A13 guard is worthless as a unit test of the SQL:
    granting graph_discoverable ITSELF enqueues build_edges, so without D16's seen_at the rebuild
    stamps last_seen_at=now and every historical interaction floats past the grant. This drives the
    real path: interact -> grant (route enqueues) -> worker runs -> the edge must STILL be pending.
    """
    client, tok, ids, _ = platform
    pid = _live_posting(client, tok)
    # (1) the interaction happens FIRST, while nobody is discoverable
    _as(client, tok, "student")
    for p in ("resume_storage", "profile_matching"):
        client.post("/api/students/me/consents", json={"purpose": p, "granted": True})
    client.post("/api/students/me/resume",
                files={"resume": ("r.txt", b"Alex. Python and SQL. " * 6, "text/plain")})
    client.post(f"/api/postings/{pid}/apply")
    # (2) both endpoints opt in afterwards; the grant route enqueues the rebuild
    for who in ("student", "employer"):
        _grant_graph(client, tok, who)
    _wait_jobs_done()
    with closing(connect()) as conn:
        edge = conn.execute("SELECT consent_state, last_seen_at FROM graph_edges "
                            "WHERE kind='application'").fetchone()
    assert edge["consent_state"] == "pending"    # opting in today does not publish last term
    # (3) a genuinely NEW interaction on the same pair advances last_seen_at past the grant, and
    #     the very next rebuild promotes it — the guard delays, it doesn't strand.
    with closing(connect()) as conn:
        conn.execute("UPDATE applications SET created_at=?", (time.time(),))
        conn.commit()
    rel = RelationshipStore()
    rel.build_native_edges(1)
    assert rel.promote_shareable(1) == 1
    with closing(connect()) as conn:
        assert conn.execute("SELECT consent_state FROM graph_edges WHERE kind='application'")\
            .fetchone()["consent_state"] == "shareable"


def test_upsert_edge_last_seen_only_advances_and_counts_real_observations():
    """D16: re-folding history must neither rewind nor inflate. observation_count is a count of
    interactions, not of rebuilds."""
    rel = RelationshipStore()
    with closing(connect()) as conn:
        rel.upsert_edge(conn, 1, 1, 2, "application", provenance="native", seen_at=1000.0)
        rel.upsert_edge(conn, 1, 1, 2, "application", provenance="native", seen_at=500.0)
        conn.commit()
        row = conn.execute("SELECT last_seen_at, observation_count FROM graph_edges").fetchone()
        assert (row["last_seen_at"], row["observation_count"]) == (1000.0, 1)   # older: no-op
        rel.upsert_edge(conn, 1, 1, 2, "application", provenance="native", seen_at=2000.0)
        conn.commit()
        row = conn.execute("SELECT last_seen_at, observation_count FROM graph_edges").fetchone()
    assert (row["last_seen_at"], row["observation_count"]) == (2000.0, 2)


# ---- Phase 5 / A14: only accepted interviews are relationships ------------------------------------
def _student(conn, uid, role="student"):
    """Seeded users get uid-derived emails — the `platform` fixture already registered the obvious
    ones and users.email is UNIQUE."""
    conn.execute("INSERT INTO users(id, email, pw_hash, salt, created_at, role, school_id) "
                 "VALUES(?,?,'h','s',?,?,1)", (uid, f"seed{uid}@york.ca", time.time(), role))


def _seed_interview(conn, *, status):
    _student(conn, 101)
    _student(conn, 102, role="employer")
    conn.execute("INSERT INTO postings(id, org_id, school_id, created_by, title, description, "
                 "status, created_at, updated_at) VALUES('p1',1,1,102,'Dev','x','live',?,?)",
                 (time.time(), time.time()))
    conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at, "
                 "updated_at) VALUES('a1','p1',101,'applied',?,?)", (time.time(), time.time()))
    conn.execute("INSERT INTO interview_slots(id, application_id, proposed_by, starts_at, "
                 "ends_at, status, created_at) VALUES('i1','a1',102,?,?,?,?)",
                 (time.time(), time.time() + 3600, status, time.time()))


@pytest.mark.parametrize("status,expected", [("accepted", 1), ("declined", 0),
                                             ("cancelled", 0), ("proposed", 0)])
def test_interview_fold_is_accepted_slots_only(platform, status, expected):
    with closing(connect()) as conn:
        _seed_interview(conn, status=status)
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='interview'")\
            .fetchone()[0] == expected


def test_folds_skip_the_erasure_sentinel(platform):
    """FM-M5: an erased employer's postings survive with created_by=0. Uid 0 is nobody — folding
    an edge to it would resurrect the erased person as a graph node."""
    with closing(connect()) as conn:
        _seed_interview(conn, status="accepted")
        conn.execute("UPDATE postings SET created_by=0")
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE user_a=0 OR user_b=0")\
            .fetchone()[0] == 0


# ---- Phase 5 / C3: peer co-attendance folds from VERIFIED presence only ---------------------------
def _seed_event(conn, n_students=3, *, checkins=True, at=None, employer=False):
    conn.execute("INSERT INTO campus_events(id, school_id, kind, title, starts_at, created_by, "
                 "status, created_at, updated_at) VALUES('e1',1,'fair','Fair',?,1,'published',?,?)",
                 (time.time(), time.time(), time.time()))
    uids = list(range(201, 201 + n_students))
    for uid in uids:
        _student(conn, uid)
        conn.execute("INSERT INTO event_registrations(event_id, user_id, role, created_at) "
                     "VALUES('e1',?,'student',?)", (uid, time.time()))
        if checkins:
            conn.execute("INSERT INTO event_checkins(event_id, user_id, checked_in_by, method, at)"
                         " VALUES('e1',?,?,'code',?)", (uid, uid, at or time.time()))
    if employer:
        _student(conn, 301, role="employer")
        conn.execute("INSERT INTO event_registrations(event_id, user_id, role, created_at) "
                     "VALUES('e1',301,'employer',?)", (time.time(),))
        if checkins:
            conn.execute("INSERT INTO event_checkins(event_id, user_id, checked_in_by, method, at)"
                         " VALUES('e1',301,301,'roster',?)", (at or time.time(),))
    return uids


def test_peer_edges_never_fold_from_rsvp(platform):
    """D9: an RSVP is an intention, not a presence. Folding peer edges from it would mint
    relationships between people who never met."""
    with closing(connect()) as conn:
        _seed_event(conn, 3, checkins=False)
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='peer_coattendance'")\
            .fetchone()[0] == 0


def test_peer_edges_fold_from_checkins_and_respect_the_cap(platform, monkeypatch):
    with closing(connect()) as conn:
        _seed_event(conn, 3)
        conn.commit()
    monkeypatch.setenv("RM_PEER_EDGE_MAX_CHECKINS", "2")   # 3 checked in -> event skipped entirely
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='peer_coattendance'")\
            .fetchone()[0] == 0
    monkeypatch.setenv("RM_PEER_EDGE_MAX_CHECKINS", "150")
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        rows = conn.execute("SELECT user_a, user_b, consent_state, provenance_ref, weight "
                            "FROM graph_edges WHERE kind='peer_coattendance'").fetchall()
    assert {(r["user_a"], r["user_b"]) for r in rows} == {(201, 202), (201, 203), (202, 203)}
    assert all(r["consent_state"] == "pending" and r["provenance_ref"] == "e1" for r in rows)


def test_peer_fold_watermark_skips_events_with_no_new_checkins(platform):
    """FM-M4: steady-state rebuilds must touch zero events, and a re-fold must not inflate
    observation_count (the D16 invariant has to hold in the set-based form too)."""
    with closing(connect()) as conn:
        _seed_event(conn, 3, at=1000.0)
        conn.commit()
    rel = RelationshipStore()
    rel.build_native_edges(1)
    rel.build_native_edges(1)
    with closing(connect()) as conn:
        counts = {r["observation_count"] for r in conn.execute(
            "SELECT observation_count FROM graph_edges WHERE kind='peer_coattendance'")}
        assert counts == {1}
        # a later check-in re-opens the event and the pair edges advance exactly once
        conn.execute("UPDATE event_checkins SET at=2000.0 WHERE user_id=201")
        conn.commit()
    rel.build_native_edges(1)
    with closing(connect()) as conn:
        rows = {(r["user_a"], r["user_b"]): (r["observation_count"], r["last_seen_at"])
                for r in conn.execute("SELECT user_a, user_b, observation_count, last_seen_at "
                                      "FROM graph_edges WHERE kind='peer_coattendance'")}
    assert rows[(201, 202)] == (2, 2000.0) and rows[(202, 203)] == (1, 1000.0)


def test_peer_fold_watermark_converges_when_the_pair_edge_came_from_another_event(platform):
    """The watermark must key off PAIR staleness, not `provenance_ref=event`. A pair that already
    met at e1 leaves e2 with no edge carrying provenance_ref='e2' (the upsert never rewrites it),
    so the old MAX(last_seen_at) probe COALESCEd to -1 and re-scanned e2 on every run forever."""
    with closing(connect()) as conn:
        _seed_event(conn, 2, at=1000.0)                  # e1: students 201,202 check in
        conn.execute("INSERT INTO campus_events(id, school_id, kind, title, starts_at, created_by, "
                     "status, created_at, updated_at) "
                     "VALUES('e2',1,'fair','Fair 2',?,1,'published',?,?)",
                     (time.time(), time.time(), time.time()))
        for uid in (201, 202):                           # ...and the same pair meets again at e2
            conn.execute("INSERT INTO event_checkins(event_id, user_id, checked_in_by, method, at)"
                         " VALUES('e2',?,?,'code',900.0)", (uid, uid))
        conn.commit()
    rel = RelationshipStore()
    rel.build_native_edges(1)
    with closing(connect()) as conn:
        # ONE edge for the one relationship, and a second run does no work at all.
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='peer_coattendance'")\
            .fetchone()[0] == 1
        assert rel._fold_peer_coattendance(conn, 1) == 0


def test_peer_fold_key_agrees_with_edge_key_in_both_orders(platform):
    """The set-based twin bypasses _edge_key; if the two disagree on ordering the UNIQUE index
    stops deduping and one relationship becomes two edges."""
    with closing(connect()) as conn:
        _seed_event(conn, 2, at=1000.0)
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        row = conn.execute("SELECT edge_key, user_a, user_b FROM graph_edges "
                           "WHERE kind='peer_coattendance'").fetchone()
    for pair in ((201, 202), (202, 201)):                # either ordering -> the same key
        a, b, key = RelationshipStore._edge_key(*pair, "peer_coattendance")
        assert (a, b, key) == (row["user_a"], row["user_b"], row["edge_key"])


def test_peer_fold_skips_suppressed_and_never_resurrects_revoked(platform):
    """The set-based upsert must hold BOTH upsert_edge invariants itself — it bypasses the Python
    helper, so a regression here would silently re-materialize tombstoned people."""
    with closing(connect()) as conn:
        _seed_event(conn, 3, at=1000.0)
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        conn.execute("UPDATE graph_edges SET consent_state='revoked', revoked_at=? "
                     "WHERE user_a=201 AND user_b=202", (time.time(),))
        conn.execute("DELETE FROM graph_edges WHERE user_a=201 AND user_b=203")
        conn.execute("INSERT INTO graph_suppressions(school_id, user_id, reason, created_at) "
                     "VALUES(1,203,'member_deleted',?)", (time.time(),))
        conn.execute("UPDATE event_checkins SET at=2000.0")          # re-open the event
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        rows = {(r["user_a"], r["user_b"]): (r["consent_state"], r["last_seen_at"])
                for r in conn.execute("SELECT user_a, user_b, consent_state, last_seen_at "
                                      "FROM graph_edges WHERE kind='peer_coattendance'")}
    assert rows[(201, 202)][0] == "revoked"          # never un-revoked, despite a newer check-in
    assert (201, 203) not in rows                    # suppressed endpoint: never re-created
    assert rows[(202, 203)] == ("pending", 1000.0)   # ...and its surviving edge is not touched


def test_verified_checkins_beat_rsvp_for_employer_coattendance(platform):
    """C3: an event with check-ins folds from presence; the RSVP fold is the fallback only."""
    with closing(connect()) as conn:
        _seed_event(conn, 2, checkins=False, employer=True)       # RSVP-only event
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='event_coattendance'")\
            .fetchone()[0] == 2      # fallback: both RSVP'd students <-> the employer
        # now only ONE student actually shows up (and so does the employer)
        conn.execute("INSERT INTO event_checkins(event_id, user_id, checked_in_by, method, at) "
                     "VALUES('e1',201,201,'code',?),('e1',301,301,'roster',?)",
                     (time.time(), time.time()))
        conn.execute("DELETE FROM graph_edges")
        conn.commit()
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        pairs = {(r["user_a"], r["user_b"]) for r in conn.execute(
            "SELECT user_a, user_b FROM graph_edges WHERE kind='event_coattendance'")}
    assert pairs == {(201, 301)}     # the no-show gets no edge


# ---- Phase 5 / A2 + SM-M2: a mutual without warm_intro is not a broker ----------------------------
def test_broker_without_warm_intro_is_indistinguishable_from_no_path(platform):
    client, tok, ids, _ = platform
    _seed_broker_path(ids)
    for who in ("student", "broker", "employer"):
        _grant_graph(client, tok, who)
    rel = RelationshipStore()
    consenting = find_paths(rel, ids["student"], ids["employer"], 1,
                            broker_ok=lambda uid: True)
    assert consenting and consenting[0]["broker"] == ids["broker"]
    # the same graph, with the broker opted out of brokering: EXACTLY the no-path response, not a
    # shorter list or a scored-zero path (either would be a consent oracle)
    assert find_paths(rel, ids["student"], ids["employer"], 1,
                      broker_ok=lambda uid: False) == []


def test_create_rechecks_broker_consent_inside_its_transaction(platform):
    """SM-M2: the route's check is advisory — a broker can revoke between pathfinding and INSERT.
    The in-transaction check is the invariant that keeps a row from ever naming a non-consenting
    broker."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    rel = RelationshipStore()
    path = find_paths(rel, ids["student"], ids["employer"], 1)[0]
    with closing(connect()) as conn:     # the broker revokes AFTER the path was computed
        conn.execute("UPDATE consents SET revoked_at=? WHERE user_id=? AND purpose='warm_intro'",
                     (time.time(), ids["broker"]))
        conn.commit()
    with pytest.raises(IntroError, match="isn't available"):
        IntroStore().create(school_id=1, posting_id=pid, application_id=app_id,
                            requester_user_id=ids["student"], target_user_id=ids["employer"],
                            path=path, note_redacted=None)
    with closing(connect()) as conn:     # and nothing was written
        assert conn.execute("SELECT COUNT(*) FROM intro_requests").fetchone()[0] == 0


# ---- Phase 5 / C2: intro origin ------------------------------------------------------------------
def test_path_origin_and_outcome_rows(platform):
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    rel = RelationshipStore()
    path = find_paths(rel, ids["student"], ids["employer"], 1)[0]
    assert path_origin(path) == "organic"
    assert path_origin({"edges": [("mentorship", 1.0), ("interview", 1.0)]}) == "bridged"
    assert path_origin({"edges": [("alumni_bridge", 1.0)]}) == "bridged"
    IntroStore().create(school_id=1, posting_id=pid, application_id=app_id,
                        requester_user_id=ids["student"], target_user_id=ids["employer"],
                        path=path, note_redacted=None, origin=path_origin(path))
    rows = IntroStore().outcome_rows(1)
    assert len(rows) == 1
    assert rows[0]["origin"] == "organic" and rows[0]["status"] == "requested"
    assert rows[0]["broker_edge_kind"] == "verified_vouch"   # the requester->broker hop
    assert rows[0]["application_status"] == "applied"
    assert "path_json" not in rows[0]        # the report never re-exports the raw path


# ==================================================================================================
# Phase 5 slice S5 — the ROUTE half of A2/C2/FM-M5 (docs/PHASE5.md §3.1)
# ==================================================================================================
def test_routes_prune_a_broker_without_warm_intro(platform):
    """A2 end-to-end. S2 gave find_paths a `broker_ok` hook; this pins that the routes actually
    PASS it — and that the result is the ordinary no-path answer, not a distinguishable one. A
    student must not be able to probe who did or didn't opt into brokering."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    _as(client, tok, "student")
    assert client.get(f"/api/intros/available/{pid}").json() == {"warm_intro_available": True}

    # the only mutual opts out of brokering (staying discoverable — the two are separate consents)
    _as(client, tok, "broker")
    client.post("/api/graph/consents", json={"purpose": "warm_intro", "granted": False})

    _as(client, tok, "student")
    # byte-identical to a school with no graph at all
    assert client.get(f"/api/intros/available/{pid}").json() == {"warm_intro_available": False}
    r = client.post("/api/intros/requests", json={"application_id": app_id})
    assert r.status_code == 409
    assert r.json()["detail"] == "No warm intro is available for this posting."
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM intro_requests").fetchone()[0] == 0


def test_intro_routes_record_origin(platform):
    """C2 through the route: `origin` is decided from the CHOSEN path, so it has to be computed
    where the path is chosen."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    _as(client, tok, "student")
    client.post("/api/intros/requests", json={"application_id": app_id})
    with closing(connect()) as conn:
        assert conn.execute("SELECT origin FROM intro_requests").fetchone()["origin"] == "organic"


def test_intro_routes_mark_a_coordinator_bridged_path(platform):
    """C2: a path leaning on an edge the institution manufactured is 'bridged'. That distinction is
    the whole point of the equity report — it is how the bridge program gets held to account."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    rel = RelationshipStore()
    with closing(connect()) as conn:   # replace the organic hop with a manufactured bridge
        conn.execute("DELETE FROM graph_edges WHERE kind='verified_vouch'")
        rel.upsert_edge(conn, 1, ids["student"], ids["broker"], "alumni_bridge",
                        provenance="alumni", consent_state="shareable")
        conn.commit()
    _as(client, tok, "student")
    assert client.post("/api/intros/requests",
                       json={"application_id": app_id}).status_code == 201
    with closing(connect()) as conn:
        assert conn.execute("SELECT origin FROM intro_requests").fetchone()["origin"] == "bridged"


def test_hiring_manager_sentinel_zero_is_nobody(platform):
    """FM-M5. An erased employer's postings survive as org records with created_by=0. Uid 0 is not a
    person: targeting it would resurrect the erased human as a pathfinding target."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    with closing(connect()) as conn:
        conn.execute("UPDATE postings SET created_by=0 WHERE id=?", (pid,))
        conn.commit()
    _as(client, tok, "student")
    assert client.get(f"/api/intros/available/{pid}").json() == {"warm_intro_available": False}
    r = client.post("/api/intros/requests", json={"application_id": app_id})
    assert r.status_code == 409          # the neutral no-intro answer, not a request naming uid 0
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM intro_requests WHERE target_user_id=0")\
            .fetchone()[0] == 0


def test_hiring_manager_prefers_the_designated_posting_contact(platform):
    """C5 preference order: an explicit posting contact beats the creator, and an employer_contacts
    row behind it resolves to its member. The creator is only the fallback."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    from resume_matcher.api.platform import _hiring_manager
    from resume_matcher.stores.platform import PostingStore

    posting = PostingStore().get(pid)
    assert _hiring_manager(posting) == ids["employer"]        # fallback: created_by

    with closing(connect()) as conn:   # a business contact pointing at a member
        conn.execute("INSERT INTO employer_contacts(id, school_id, org_id, display_label, "
                     "contact_user_id, added_by, created_at) VALUES('ec1',1,1,'Dana Lee',?,?,?)",
                     (ids["broker"], ids["employer"], time.time()))
        conn.execute("INSERT INTO posting_contacts(id, school_id, posting_id, "
                     "employer_contact_id, added_by, created_at) VALUES('pc1',1,?,'ec1',?,?)",
                     (pid, ids["employer"], time.time()))
        conn.commit()
    assert _hiring_manager(PostingStore().get(pid)) == ids["broker"]

    with closing(connect()) as conn:   # a direct contact_user_id wins outright
        conn.execute("UPDATE posting_contacts SET contact_user_id=? WHERE id='pc1'",
                     (ids["coord"],))
        conn.commit()
    assert _hiring_manager(PostingStore().get(pid)) == ids["coord"]


def test_evidence_card_view_is_exposure_logged_once(platform):
    """RELATIONSHIPS.md:395 — seeing relationship evidence about a candidate is the same
    AEDT-relevant moment the shortlist view logs, so it lands in the same append-only log."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    _as(client, tok, "employer")
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE action='shortlist_exposed'")\
            .fetchone()[0] == 0
    assert client.get(f"/api/intros/for-application/{app_id}").status_code == 200
    client.get(f"/api/intros/for-application/{app_id}")          # a second look is the same look
    with closing(connect()) as conn:
        rows = conn.execute("SELECT actor_user_id, entity, entity_id FROM events "
                            "WHERE action='shortlist_exposed'").fetchall()
    assert len(rows) == 1
    assert (rows[0]["actor_user_id"], rows[0]["entity"], rows[0]["entity_id"]) \
        == (ids["employer"], "posting", pid)


def test_accepted_intro_notifies_the_requester_but_a_decline_notifies_nobody(platform):
    """D8, the silent-decline invariant extended to notifications: a declined intro must stay
    indistinguishable from 'no path was ever found'."""
    client, tok, ids, _ = platform
    pid, app_id = _live_posting_with_application(client, tok, ids)
    _as(client, tok, "student")
    intro_id = client.post("/api/intros/requests",
                           json={"application_id": app_id}).json()["intro_id"]
    with closing(connect()) as conn:   # the broker learns they were asked; nobody else does
        kinds = [r["kind"] for r in conn.execute(
            "SELECT user_id, kind FROM notifications WHERE user_id=?", (ids["broker"],))]
    assert kinds == ["intro_request"]

    _as(client, tok, "broker")
    client.post(f"/api/intros/requests/{intro_id}/decline")
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=?",
                            (ids["student"],)).fetchone()[0] == 0

    # ...whereas an ACCEPT does notify the requester (the reveal they opted into)
    _as(client, tok, "student")
    with closing(connect()) as conn:
        conn.execute("DELETE FROM intro_requests")
        conn.commit()
    intro_id = client.post("/api/intros/requests",
                           json={"application_id": app_id}).json()["intro_id"]
    _as(client, tok, "broker")
    client.post(f"/api/intros/requests/{intro_id}/accept", json={"relationship": "classmate"})
    with closing(connect()) as conn:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM notifications WHERE user_id=?", (ids["student"],))]
    assert kinds == ["intro_accepted"]
