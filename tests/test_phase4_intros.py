"""Slices AC/AD/AE/AF: native edge builder, consent-gated pathfinder, double-opt-in intro flow
(incl. the two adversarial CRITICALS: no self-accept, IDOR blocked), and vouches."""
from __future__ import annotations

from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402
from resume_matcher.stores.intros import edge_score, find_paths, rank_path  # noqa: E402
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
