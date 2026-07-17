"""Phase-5 API surface (api/phase5.py, docs/PHASE5.md §3.2/§3.3).

The route-level half of the spec's hard requirements: the claimants gate + masked emails (P-F1),
school-scoping everywhere with cross-tenant = 404 (SC-C1/SC-C2), claim_role as display-only (D14),
no grad_year anywhere (P-F2), the silent mentorship decline (D8/P-F9), the access-logged
under-networked roster (P-F7), the check-in second-factor limiter (SM-M3), invite-submit redaction
(SM-M6), and the password step-up on the irreversible DELETE /api/account (SL-L3).
"""
from __future__ import annotations

import time
from contextlib import closing

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from resume_matcher.api import phase5 as phase5_mod  # noqa: E402
from resume_matcher.api.accounts import get_account_store  # noqa: E402
from resume_matcher.api.app import create_app  # noqa: E402
from resume_matcher.api.platform import _hiring_manager  # noqa: E402
from resume_matcher.stores.db import connect  # noqa: E402
from resume_matcher.stores.notifications import NotificationStore  # noqa: E402
from resume_matcher.stores.students import StudentStore  # noqa: E402


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_PLATFORM_EXTRACT_BACKEND", "mock")
    # the check-in limiter is module-level (process-local by design); give each test a fresh bucket
    # so ids reused across tmp DBs can't leak throttling into one another
    monkeypatch.setattr(phase5_mod, "_checkin_rate", phase5_mod._Rate(5, 5 / 600))
    accounts = get_account_store()
    with closing(connect()) as conn:  # school 1 = seeded York; add school 2
        conn.execute("INSERT INTO schools(id, name, created_at) VALUES(2, 'Seneca', ?)",
                     (time.time(),))
        conn.commit()
    t, uid = {}, {}
    t["employer"], _ = accounts.register("hr@acme.com", "password123", role="employer",
                                         org_name="Acme Corp")
    t["studentA"], _ = accounts.register("a@york.ca", "password123")
    t["studentA2"], _ = accounts.register("a2@york.ca", "password123")
    t["alum"], _ = accounts.register("alum@york.ca", "password123")
    t["studentB"], _ = accounts.register("b@seneca.ca", "password123", school_id=2)
    t["coordA"], _ = accounts.create_user("ca@york.ca", "password123", role="coordinator")
    t["coordB"], _ = accounts.create_user("cb@seneca.ca", "password123", role="coordinator",
                                          school_id=2)
    for who, token in t.items():
        uid[who] = accounts.user_for_token(token)["id"]
    org_id = accounts.user_for_token(t["employer"])["org_id"]
    return TestClient(create_app()), t, uid, org_id


def _as(client, tokens, who):
    client.cookies.set("rm_session", tokens[who])
    return client


def _consent(uid: int, *purposes: str) -> None:
    store = StudentStore()
    for purpose in purposes:
        store.set_consent(uid, purpose, True)


def _make_mentor(uid: int, program: str = "computer science", school_id: int = 1) -> None:
    """A mentor must clear every gate: verified alum standing + both consents + an active profile."""
    StudentStore().set_alumni_status(uid, school_id, "verified")
    _consent(uid, "warm_intro", "graph_discoverable")


def _event(client, tokens, who="coordA") -> str:
    _as(client, tokens, who)
    eid = client.post("/api/events", json={"title": "Fall Fair", "starts_at": time.time() + 3600}
                      ).json()["event_id"]
    assert client.patch(f"/api/events/{eid}", json={"status": "published"}).status_code == 200
    return eid


# ---- notifications ---------------------------------------------------------------------------------
def test_notification_feed_and_mark_read_are_user_scoped(api):
    """SL-L1: every read and every UPDATE carries WHERE user_id=? — a supplied ids list from user B
    can never touch user A's rows."""
    client, t, uid, _ = api
    store = NotificationStore()
    mine = store.notify(uid["studentA"], 1, "message", "A new message", "Open the thread.")
    theirs = store.notify(uid["studentA2"], 1, "message", "A new message", "Open the thread.")

    _as(client, t, "studentA")
    feed = client.get("/api/notifications").json()
    assert [i["id"] for i in feed["items"]] == [mine] and feed["unread"] == 1

    # A tries to mark B's row read: the id is simply not theirs, so nothing happens
    assert client.post("/api/notifications/read", json={"ids": [theirs]}).json()["marked"] == 0
    _as(client, t, "studentA2")
    assert client.get("/api/notifications?unread=1").json()["unread"] == 1

    _as(client, t, "studentA")
    assert client.post("/api/notifications/read", json={"all": True}).json()["marked"] == 1
    assert client.get("/api/notifications?unread=1").json()["unread"] == 0
    assert client.get("/api/notifications").json()["items"][0]["read_at"] is not None


def test_notifications_require_a_session(api):
    client, t, uid, _ = api
    client.cookies.clear()
    assert client.get("/api/notifications").status_code == 401


# ---- C4: mentorship --------------------------------------------------------------------------------
def test_mentor_profile_needs_standing_and_warm_intro_consent(api):
    client, t, uid, _ = api
    _as(client, t, "studentA")
    # a plain student (not a verified alum) has no mentor standing
    assert client.put("/api/mentorship/profile", json={"program": "CS"}).status_code == 403

    StudentStore().set_alumni_status(uid["alum"], 1, "self_claimed")
    _as(client, t, "alum")
    assert client.put("/api/mentorship/profile", json={"program": "CS"}).status_code == 403  #
    StudentStore().set_alumni_status(uid["alum"], 1, "verified")
    # verified, but the opt-in would never fire without warm_intro -> say why instead of accepting
    r = client.put("/api/mentorship/profile", json={"program": "CS"})
    assert r.status_code == 409 and "warm intros" in r.json()["detail"]

    _consent(uid["alum"], "warm_intro")
    r = client.put("/api/mentorship/profile", json={"program": "CS", "capacity": 2})
    assert r.status_code == 200 and r.json()["capacity"] == 2
    assert client.delete("/api/mentorship/profile").json()["deleted"] is True


def test_mentorship_is_double_opt_in_and_the_decline_is_silent(api):
    """D8/P-F9: the mentor decides; the student is never told it was declined, and NO coordinator
    surface carries per-offer status — only MIN_CELL'd aggregates."""
    client, t, uid, _ = api
    _make_mentor(uid["alum"])
    _as(client, t, "alum")
    client.put("/api/mentorship/profile", json={"program": "computer science", "capacity": 3})

    _as(client, t, "coordA")
    assert [m["user_id"] for m in client.get("/api/coordinator/mentors").json()["mentors"]] \
        == [uid["alum"]]
    r = client.post("/api/coordinator/mentorship-offers",
                    json={"student_id": uid["studentA"], "mentor_id": uid["alum"]})
    assert r.status_code == 202 and r.json() == {"status": "offered"}   # no id, nothing to poll

    # the mentor's inbox is where (and only where) the student's identity is revealed
    _as(client, t, "alum")
    offers = client.get("/api/mentorship/offers").json()["offers"]
    assert len(offers) == 1 and offers[0]["student_email"] == "a@york.ca"
    offer_id = offers[0]["id"]

    # a NON-mentor cannot respond, and probing the id reads as absent
    _as(client, t, "studentA2")
    assert client.post(f"/api/mentorship/offers/{offer_id}/respond",
                       json={"accept": True}).status_code == 404

    _as(client, t, "alum")
    assert client.post(f"/api/mentorship/offers/{offer_id}/respond",
                       json={"accept": False}).json() == {"status": "declined"}
    # the student hears nothing at all
    _as(client, t, "studentA")
    assert client.get("/api/notifications").json()["items"] == []
    # ... and the coordinator's only telemetry is aggregates, suppressed under MIN_CELL
    _as(client, t, "coordA")
    stats = client.get("/api/coordinator/mentorship-stats").json()
    assert set(stats) == {"offers_made", "accepted", "active_mentors", "min_cell"}
    assert stats["offers_made"] is None and stats["accepted"] is None
    body = client.get("/api/coordinator/mentorship-stats").text
    assert offer_id not in body and "declined" not in body

    # re-offering the same pair is refused with the SAME message an open offer gets (cooldown) —
    # otherwise the coordinator could probe the decline
    r = client.post("/api/coordinator/mentorship-offers",
                    json={"student_id": uid["studentA"], "mentor_id": uid["alum"]})
    assert r.status_code == 409 and "isn't available" in r.json()["detail"]


def test_mentorship_accept_mints_an_edge_and_tells_the_student(api):
    client, t, uid, _ = api
    _make_mentor(uid["alum"])
    _consent(uid["studentA"], "graph_discoverable")
    _as(client, t, "alum")
    client.put("/api/mentorship/profile", json={"program": "computer science"})
    _as(client, t, "coordA")
    client.post("/api/coordinator/mentorship-offers",
                json={"student_id": uid["studentA"], "mentor_id": uid["alum"]})
    _as(client, t, "alum")
    offer_id = client.get("/api/mentorship/offers").json()["offers"][0]["id"]
    assert client.post(f"/api/mentorship/offers/{offer_id}/respond",
                       json={"accept": True}).json() == {"status": "accepted"}
    with closing(connect()) as conn:
        edge = conn.execute("SELECT kind, consent_state FROM graph_edges WHERE provenance_ref=?",
                            (offer_id,)).fetchone()
    assert edge["kind"] == "mentorship"
    _as(client, t, "studentA")
    kinds = [i["kind"] for i in client.get("/api/notifications").json()["items"]]
    assert kinds == ["mentorship_accepted"]
    # the offer is terminal: a second response reads as absent
    _as(client, t, "alum")
    assert client.post(f"/api/mentorship/offers/{offer_id}/respond",
                       json={"accept": False}).status_code == 404


# ---- C4: alumni ------------------------------------------------------------------------------------
def test_alumni_claim_and_verification_never_touch_grad_year(api):
    """SC-C2 + P-F2: a student can only self-CLAIM (never self-verify), and grad_year appears in no
    request, response, or queue — coordinators attest against SIS by email, out of band."""
    client, t, uid, _ = api
    _as(client, t, "studentA")
    r = client.post("/api/alumni/claim")
    assert r.status_code == 200 and r.json() == {"alumni_status": "self_claimed"}
    # the claim route takes no body at all: there is no field to smuggle a grad year through
    assert client.post("/api/alumni/claim", json={"alumni_status": "verified",
                                                  "grad_year": 2019}).json()["alumni_status"] \
        == "self_claimed"
    # a student cannot reach the coordinator verification surface
    assert client.post(f"/api/coordinator/alumni/{uid['studentA']}/verify",
                       json={"approve": True}).status_code == 403

    _as(client, t, "coordA")
    queue = client.get("/api/coordinator/alumni")
    assert "grad_year" not in queue.text
    assert [c["user_id"] for c in queue.json()["claims"]] == [uid["studentA"]]
    assert set(queue.json()["claims"][0]) == {"user_id", "email", "program"}

    r = client.post(f"/api/coordinator/alumni/{uid['studentA']}/verify", json={"approve": True})
    assert r.json()["alumni_status"] == "verified"
    # the attestation record is the append-only events row
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE action='alumni_verified' "
                            "AND entity_id=?", (str(uid["studentA"]),)).fetchone()[0] == 1
    assert client.get("/api/coordinator/alumni").json()["claims"] == []
    # denial puts them back to 'none'
    client.post(f"/api/coordinator/alumni/{uid['studentA']}/verify", json={"approve": True})
    assert client.post(f"/api/coordinator/alumni/{uid['studentA']}/verify",
                       json={"approve": False}).json()["alumni_status"] == "none"


# ---- C1: under-networked roster --------------------------------------------------------------------
def test_under_networked_takes_the_analytics_consent_and_logs_every_read(api):
    """FH-H3: an individual-level roster is analytics ABOUT a person, so it needs network_analytics
    on top of graph_discoverable. P-F7: every read is access-logged; P-F2: no grad_year; the
    trigger is structural (zero shareable edges), never a self-ID field."""
    client, t, uid, _ = api
    _consent(uid["studentA"], "graph_discoverable")               # discoverable but no analytics
    _consent(uid["studentA2"], "graph_discoverable", "network_analytics")

    _as(client, t, "coordA")
    body = client.get("/api/coordinator/under-networked")
    assert body.status_code == 200
    listed = body.json()
    assert [s["user_id"] for s in listed["students"]] == [uid["studentA2"]]
    assert listed["total"] == 1
    assert set(listed["students"][0]) == {"user_id", "email", "program", "degree"}
    assert listed["students"][0]["degree"] == 0
    assert "grad_year" not in body.text and "self_id" not in body.text

    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE action='under_networked_viewed' "
                            "AND actor_user_id=?", (uid["coordA"],)).fetchone()[0] == 1
        client.get("/api/coordinator/under-networked")
        # EVERY read, not just the first: this roster's access log is the program's shut-off record
        assert conn.execute("SELECT COUNT(*) FROM events WHERE action='under_networked_viewed'"
                            ).fetchone()[0] == 2

    _as(client, t, "studentA2")
    assert client.get("/api/coordinator/under-networked").status_code == 403


def test_mentor_match_handler_cannot_reach_the_audit_plane(api):
    """§3.3: the C4 matcher's triggers are structural, so the handler pulls in NO imports at all,
    and this module never links the two planes at import time (boundary #2)."""
    import ast
    import inspect

    handler = ast.parse(inspect.getsource(phase5_mod._mentor_match_job))
    assert not [n for n in ast.walk(handler)
                if isinstance(n, (ast.Import, ast.ImportFrom))]
    module = ast.parse(inspect.getsource(phase5_mod))
    top_level = " ".join(n.module or "" for n in module.body if isinstance(n, ast.ImportFrom))
    assert "audit_store" not in top_level and "data_planes" not in top_level


# ---- C2: intro outcomes ----------------------------------------------------------------------------
def test_intro_outcomes_is_aggregate_only_and_min_celled(api):
    client, t, uid, _ = api
    _as(client, t, "coordA")
    r = client.get("/api/coordinator/reports/intro-outcomes")
    assert r.status_code == 200
    body = r.json()
    assert body["min_cell"] == 5
    assert set(body["by_origin"]["by_origin"]) == {"organic", "bridged"}
    # an empty cohort publishes no rates and no ratios at all
    assert body["by_origin"]["by_origin"]["bridged"]["stages"]["accepted"]["rate"] is None
    assert body["by_origin"]["bridged_over_organic"]["hired"]["ratio"] is None
    csv = client.get("/api/coordinator/reports/intro-outcomes?format=csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "origin,stage,n,rate" in csv.text
    _as(client, t, "studentA")
    assert client.get("/api/coordinator/reports/intro-outcomes").status_code == 403


def test_intro_outcomes_never_serves_a_sub_min_cell_count(api, monkeypatch, tmp_path):
    """REGRESSION: the empty-cohort test above passes trivially at requested==0. The leaking state is
    a NON-ZERO cell under MIN_CELL — 3 bridged intros, 1 hired — which the route used to serve as
    {"requested": 3, "stages": {"hired": {"n": 1, "rate": null}}} under a docstring promising every
    cell was MIN_CELL'd. With the named under-networked roster beside it, that n=1 is a person."""
    client, t, uid, _ = api
    # the A8 snapshot store is NOT tmp-isolated by conftest; pin it here or a snapshot left by an
    # earlier test is served instead of the cohort under test
    monkeypatch.setenv("RM_AUDIT_DB", str(tmp_path / "audit.db"))
    rows = [{"requester_user_id": uid["studentA"], "origin": "bridged", "broker_edge_kind": "alumni",
             "status": "accepted", "application_status": "hired" if i == 0 else "applied"}
            for i in range(3)]

    class _Intros:
        def outcome_rows(self, school):
            return list(rows)

    class _Students:
        def filter_by_consent(self, refs, kind):
            return list(refs)

    monkeypatch.setattr(phase5_mod, "IntroStore", _Intros)
    monkeypatch.setattr(phase5_mod, "StudentStore", _Students)
    _as(client, t, "coordA")
    body = client.get("/api/coordinator/reports/intro-outcomes").json()
    bridged = body["by_origin"]["by_origin"]["bridged"]
    assert bridged["requested"] is None
    assert bridged["stages"]["hired"] == {"n": None, "rate": None}
    assert bridged["stages"]["accepted"] == {"n": None, "rate": None}
    assert body["by_broker_kind"]["alumni"]["hired"] is None

    # ...and the CSV export renders each suppressed cell as an EMPTY field, never the count
    csv_text = client.get("/api/coordinator/reports/intro-outcomes?format=csv").text
    import csv as _csv
    rows_out = list(_csv.reader(csv_text.splitlines()))[1:]
    assert rows_out and all(r[2] == "" and r[3] == "" for r in rows_out)


# ---- C3: check-ins ---------------------------------------------------------------------------------
def test_checkin_by_code_requires_registration_and_is_rate_limited(api):
    """SM-M3: the code is low-entropy and shouted across a room, so it is only ever the SECOND
    factor on top of the caller's own registration — and the attempts are capped per caller/IP."""
    client, t, uid, _ = api
    eid = _event(client, t)
    code = client.post(f"/api/events/{eid}/checkin-code").json()["code"]

    _as(client, t, "studentA")
    # not registered: the same neutral message as a closed event (no attendance oracle)
    assert client.post(f"/api/events/{eid}/checkin", json={"code": code}).status_code == 409
    client.post(f"/api/events/{eid}/register")
    assert client.post(f"/api/events/{eid}/checkin", json={"code": "wrong"}).status_code == 409
    assert client.post(f"/api/events/{eid}/checkin", json={"code": code}).json() == {"ok": True}

    codes = [client.post(f"/api/events/{eid}/checkin", json={"code": "nope"}).status_code
             for _ in range(6)]
    assert 429 in codes   # the bucket (5) drains long before a 6-char code can be guessed

    _as(client, t, "coordA")
    roster = client.get(f"/api/events/{eid}/checkins").json()["checkins"]
    assert [(c["user_id"], c["method"]) for c in roster] == [(uid["studentA"], "code")]


def test_roster_checkin_is_school_scoped(api):
    """SC-C1/SM-M3: a coordinator must not attest presence in a tenant they don't administer."""
    client, t, uid, _ = api
    eid = _event(client, t)
    _as(client, t, "coordB")   # school 2 coordinator, school 1 event
    assert client.post(f"/api/coordinator/events/{eid}/checkins",
                       json={"user_id": uid["studentA"]}).status_code == 404
    assert client.post(f"/api/events/{eid}/checkin-code").status_code == 404
    assert client.get(f"/api/events/{eid}/checkins").json()["checkins"] == []

    _as(client, t, "coordA")
    assert client.post(f"/api/coordinator/events/{eid}/checkins",
                       json={"user_id": uid["studentB"]}).status_code == 404   # foreign student
    assert client.post(f"/api/coordinator/events/{eid}/checkins",
                       json={"user_id": uid["studentA"]}).json() == {"ok": True}


# ---- C6: affiliations ------------------------------------------------------------------------------
def _claim(client, tokens, who, label="CSC369", kind="course_section", term="W26") -> dict:
    _as(client, tokens, who)
    r = client.post("/api/affiliations/claim",
                    json={"kind": kind, "label": label, "term": term, "claim_role": "member"})
    assert r.status_code == 201
    return r.json()


def test_claimants_needs_a_confirmed_claim_and_masks_emails(api):
    """P-F1 (hard requirement): an unconfirmed claim grants ZERO visibility — otherwise anyone could
    type a course code and harvest the roster's addresses. Full emails only inside an attestation
    pair, which by construction already know each other."""
    client, t, uid, _ = api
    a = _claim(client, t, "studentA")
    a2 = _claim(client, t, "studentA2")
    assert a["affiliation_id"] == a2["affiliation_id"]   # same normalized section
    aff = a["affiliation_id"]

    # unconfirmed claimant: the affiliation may as well not exist
    _as(client, t, "studentA")
    assert client.get(f"/api/affiliations/{aff}/claimants").status_code == 404
    # a non-claimant likewise
    _as(client, t, "alum")
    assert client.get(f"/api/affiliations/{aff}/claimants").status_code == 404

    # the confirm-LINK is what the claimant shares out of band (D15); mutual bootstrap takes both
    _as(client, t, "studentA2")
    assert client.post(f"/api/affiliations/claims/{a['claim_id']}/confirm").json() == {
        "status": "unconfirmed"}
    _as(client, t, "studentA")
    assert client.post(f"/api/affiliations/claims/{a2['claim_id']}/confirm").json() == {
        "status": "confirmed"}

    _as(client, t, "studentA")
    rows = client.get(f"/api/affiliations/{aff}/claimants").json()["claimants"]
    assert len(rows) == 1 and rows[0]["claim_role"] == "member"
    assert rows[0]["email_masked"] == "a2@york.ca"    # attestation pair: they already know

    # a third, confirmed-by-nobody claimant still sees nothing
    third = _claim(client, t, "alum")
    _as(client, t, "alum")
    assert client.get(f"/api/affiliations/{aff}/claimants").status_code == 404
    # ... and once A2 attests them, A — who is NOT in that pair — sees them MASKED
    _as(client, t, "studentA2")
    assert client.post(f"/api/affiliations/claims/{third['claim_id']}/confirm").json() == {
        "status": "confirmed"}
    _as(client, t, "studentA")
    emails = {r["email_masked"] for r in
              client.get(f"/api/affiliations/{aff}/claimants").json()["claimants"]}
    assert emails == {"a2@york.ca", "a***@york.ca"}   # pair in full, stranger masked
    assert "alum@york.ca" not in emails


def test_affiliation_claim_confirm_notifies_only_the_owner_and_delete_is_self_serve(api):
    client, t, uid, _ = api
    a = _claim(client, t, "studentA")
    a2 = _claim(client, t, "studentA2")
    _as(client, t, "studentA2")
    client.post(f"/api/affiliations/claims/{a['claim_id']}/confirm")
    _as(client, t, "studentA")
    client.post(f"/api/affiliations/claims/{a2['claim_id']}/confirm")

    # §2.11: the claim OWNER hears; there is no broadcast to co-claimants (an F1 aggravator)
    _as(client, t, "studentA2")
    kinds = [i["kind"] for i in client.get("/api/notifications").json()["items"]]
    assert kinds == ["affiliation_confirmed"]

    _as(client, t, "studentA")
    mine = client.get("/api/affiliations/mine").json()["claims"]
    assert len(mine) == 1 and mine[0]["confirm_url"].endswith(a["claim_id"])
    assert mine[0]["confirmed_by"] is True   # a boolean: the attester is never named here
    # you can only delete your own claim
    _as(client, t, "alum")
    assert client.delete(f"/api/affiliations/claims/{a['claim_id']}").json() == {"deleted": False}
    _as(client, t, "studentA")
    assert client.delete(f"/api/affiliations/claims/{a['claim_id']}").json() == {"deleted": True}


def test_self_confirm_is_refused_and_claim_role_confers_nothing(api):
    """D14/P-F4: claim_role is display-only — an "instructor" claim must not manufacture a tier."""
    client, t, uid, _ = api
    a = _claim(client, t, "studentA")
    _as(client, t, "studentA")
    r = client.post(f"/api/affiliations/claims/{a['claim_id']}/confirm")
    assert r.status_code == 400 and "your own" in r.json()["detail"]

    _as(client, t, "studentA2")
    r = client.post("/api/affiliations/claim",
                    json={"kind": "course_section", "label": "CSC369", "term": "W26",
                          "claim_role": "instructor"})
    assert r.status_code == 201
    body = client.get("/api/affiliations/mine").text
    assert "instructor" in body                       # displayed...
    assert "suggested_tier" not in body and "coordinator" not in body   # ...and nothing more


# ---- C7: vouch invites -----------------------------------------------------------------------------
def test_vouch_invite_end_to_end_hashed_redacted_and_school_scoped(api):
    """C7/D10 + FL-L2 + SM-M1 + SM-M6: link tokens (never member search), sha256 at rest, a
    cross-school voucher is rejected, and the submit body is redacted in the handler."""
    client, t, uid, _ = api
    _as(client, t, "studentA")
    r = client.post("/api/vouches/invites", json={"relationship_hint": "classmate"})
    assert r.status_code == 201 and set(r.json()) == {"invite_url", "expires_at"}
    token = r.json()["invite_url"].split("=", 1)[1]

    with closing(connect()) as conn:
        stored = conn.execute("SELECT token_hash FROM vouch_invites").fetchall()
    assert len(stored) == 1 and token not in stored[0]["token_hash"]   # only the digest at rest
    assert client.get("/api/vouches/invites").json()["invites"][0]["relationship_hint"] \
        == "classmate"

    # a voucher from another school cannot even read the link (absent, not forbidden)
    _as(client, t, "studentB")
    assert client.get(f"/api/vouches/invites/{token}").status_code == 404
    assert client.post(f"/api/vouches/invites/{token}/submit",
                       json={"relationship": "classmate", "evidence": "x"}).status_code == 404

    _as(client, t, "studentA2")
    seen = client.get(f"/api/vouches/invites/{token}").json()
    assert seen["subject_email"] == "a@york.ca" and seen["relationship_hint"] == "classmate"
    r = client.post(f"/api/vouches/invites/{token}/submit",
                    json={"relationship": "classmate",
                          "evidence": "Reach me at leak@example.com or 416-555-0199."})
    assert r.status_code == 201
    assert set(r.json()) == {"vouch_id"} and "suggested_tier" not in r.text   # D14

    _as(client, t, "studentA")
    vouches = client.get("/api/vouches/about-me").json()["vouches"]
    assert len(vouches) == 1 and vouches[0]["verify_level"] == "self"
    assert "leak@example.com" not in vouches[0]["evidence_redacted"]
    assert "416-555-0199" not in vouches[0]["evidence_redacted"]
    kinds = [i["kind"] for i in client.get("/api/notifications").json()["items"]]
    assert kinds == ["vouch_received"]

    # the link is single-use, and the subject can revoke an unused one
    _as(client, t, "alum")
    assert client.get(f"/api/vouches/invites/{token}").status_code == 404
    _as(client, t, "studentA")
    token2 = client.post("/api/vouches/invites", json={}).json()["invite_url"].split("=", 1)[1]
    assert client.delete(f"/api/vouches/invites/{token2}").json() == {"revoked": True}
    _as(client, t, "studentA2")
    assert client.get(f"/api/vouches/invites/{token2}").status_code == 404


# ---- B10: coordinator vouch queue ------------------------------------------------------------------
def _contested_vouch(client, tokens, subject_id: int) -> str:
    _as(client, tokens, "studentA2")
    vid = client.post("/api/vouches", json={"subject_user_id": subject_id,
                                            "relationship": "classmate",
                                            "evidence": "We shipped a project."}).json()["vouch_id"]
    _as(client, tokens, "studentA")
    client.post(f"/api/vouches/{vid}/contest", json={"note": "Never met them."})
    return vid


def test_coordinator_vouch_queue_resolves_and_is_school_scoped(api):
    client, t, uid, _ = api
    vid = _contested_vouch(client, t, uid["studentA"])

    _as(client, t, "coordA")
    queue = client.get("/api/coordinator/vouches?status=contested").json()["vouches"]
    assert [v["id"] for v in queue] == [vid]
    assert queue[0]["contested_note"] == "Never met them."
    assert queue[0]["voucher_email"] == "a2@york.ca" and queue[0]["subject_email"] == "a@york.ca"

    # SC-C1: the other tenant's coordinator sees nothing and can resolve nothing
    _as(client, t, "coordB")
    assert client.get("/api/coordinator/vouches?status=contested").json()["vouches"] == []
    assert client.post(f"/api/vouches/{vid}/resolve", json={"action": "verify"}).status_code == 404

    _as(client, t, "coordA")
    assert client.post(f"/api/vouches/{vid}/resolve", json={"action": "bogus"}).status_code == 400
    assert client.post(f"/api/vouches/{vid}/resolve",
                       json={"action": "verify"}).json()["verify_level"] == "coordinator"
    assert client.get("/api/coordinator/vouches?status=contested").json()["vouches"] == []


# ---- C5: contacts, posting contact, ERM ------------------------------------------------------------
def _posting(client, tokens, org_approved=True) -> str:
    _as(client, tokens, "employer")
    pid = client.post("/api/postings", json={
        "fields": {"title": "Data Analyst", "description": "Python and SQL."},
        "skills": [{"skill_id": "python", "bucket": "required"}]}).json()["posting_id"]
    return pid


def test_org_contacts_crud_cascade_and_hiring_manager_preference(api):
    """P-F5: contact free text is capped + redacted at write. The C5 deletion path takes the
    posting_contacts rows with it, so a deleted contact stops steering the pathfinder."""
    client, t, uid, org_id = api
    pid = _posting(client, t)
    # a second employer in the SAME org — the real hiring manager, who did not paste the JD
    hm_token, _ = get_account_store().register("hm@acme.com", "password123", role="employer",
                                               org_name="Acme Corp")
    hm_id = get_account_store().user_for_token(hm_token)["id"]

    _as(client, t, "employer")
    r = client.post("/api/orgs/me/contacts",
                    json={"display_label": "Hiring Manager, mail dana@acme.com",
                          "role_title": "Engineering Manager"})
    assert r.status_code == 201
    cid = r.json()["contact_id"]
    assert "dana@acme.com" not in r.json()["display_label"]   # redacted at ingest, not at render
    listed = client.get("/api/orgs/me/contacts").json()["contacts"]
    assert [c["contact_id"] for c in listed] == [cid]

    # SM-M4: a contact_user_id must be an own-org member
    assert client.post("/api/orgs/me/contacts",
                       json={"display_label": "Ghost",
                             "contact_user_id": uid["studentA"]}).status_code == 400

    # the pathfinder falls back to created_by until a contact is named...
    with closing(connect()) as conn:
        posting = dict(conn.execute("SELECT * FROM postings WHERE id=?", (pid,)).fetchone())
    assert _hiring_manager(posting) == uid["employer"]
    # ... and a named own-org USER takes precedence over whoever created the posting
    r = client.put(f"/api/postings/{pid}/contact",
                   json={"contact_user_id": hm_id, "relation": "hiring_manager"})
    assert r.status_code == 200 and r.json()["relation"] == "hiring_manager"
    assert _hiring_manager(posting) == hm_id != uid["employer"]

    # one target per posting: naming the free-text contact REPLACES the user row
    r = client.put(f"/api/postings/{pid}/contact",
                   json={"employer_contact_id": cid, "relation": "hiring_manager"})
    assert r.status_code == 200
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts WHERE posting_id=?",
                            (pid,)).fetchone()[0] == 1

    # THE C5 deletion path: the contact AND every posting_contacts row pointing at it
    assert client.delete(f"/api/orgs/me/contacts/{cid}").json() == {"deleted": True}
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts WHERE posting_id=?",
                            (pid,)).fetchone()[0] == 0
    assert client.delete(f"/api/orgs/me/contacts/{cid}").json() == {"deleted": False}
    assert client.delete(f"/api/postings/{pid}/contact").json() == {"deleted": False}

    _as(client, t, "studentA")
    assert client.get("/api/orgs/me/contacts").status_code == 403


def test_posting_contact_is_own_org_only(api):
    client, t, uid, org_id = api
    pid = _posting(client, t)
    other, _ = get_account_store().register("hr@other.com", "password123", role="employer",
                                            org_name="Other Inc")
    client.cookies.set("rm_session", other)
    assert client.put(f"/api/postings/{pid}/contact",
                      json={"contact_user_id": uid["employer"]}).status_code == 403
    assert client.delete(f"/api/postings/{pid}/contact").status_code == 403


def test_erm_rollup_is_org_level_only(api):
    client, t, uid, org_id = api
    _posting(client, t)
    _as(client, t, "coordA")
    r = client.get("/api/coordinator/orgs")
    assert r.status_code == 200
    orgs = r.json()["orgs"]
    assert [o["name"] for o in orgs] == ["Acme Corp"]
    assert orgs[0]["postings_total"] == 1 and orgs[0]["postings_live"] == 0
    assert "a@york.ca" not in r.text   # counts only: no student identity crosses this surface
    _as(client, t, "coordB")
    assert client.get("/api/coordinator/orgs").json()["orgs"] == []


# ---- A3: self-serve erasure ------------------------------------------------------------------------
def test_delete_account_requires_the_password_step_up(api):
    """SL-L3: an irreversible, cross-plane erasure behind nothing but a session cookie is not
    acceptable — the account's own PBKDF2 hash is the gate."""
    client, t, uid, _ = api
    _as(client, t, "studentA")
    NotificationStore().notify(uid["studentA"], 1, "message", "Hi", "there")

    assert client.request("DELETE", "/api/account", json={}).status_code == 400
    assert client.request("DELETE", "/api/account",
                          json={"confirm_email": "someone@else.ca",
                                "password": "password123"}).status_code == 400
    r = client.request("DELETE", "/api/account",
                       json={"confirm_email": "a@york.ca", "password": "wrong-password"})
    assert r.status_code == 403 and "password" in r.json()["detail"].lower()
    with closing(connect()) as conn:   # nothing happened
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?",
                            (uid["studentA"],)).fetchone()[0] == 1

    r = client.request("DELETE", "/api/account",
                       json={"confirm_email": "A@York.ca ", "password": "password123"})
    assert r.status_code == 200 and r.json()["erased"] is True
    assert r.json()["tables"]["users"] == 1
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?",
                            (uid["studentA"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=?",
                            (uid["studentA"],)).fetchone()[0] == 0
    # the session died with the tokens row
    assert client.get("/api/notifications").status_code == 401


# ---- gates: the admin wall, and the public page ----------------------------------------------------
def test_account_and_repudiate_are_not_behind_the_admin_gate(tmp_path, monkeypatch):
    """FM-M2: without the bare "/api/account" prefix the admin gate 401s DELETE /api/account before
    require_role ever runs. FM-M3/B3: /repudiate is public — a non-member has no account."""
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_ADMIN_PASSWORD", "a-strong-admin-password")
    token, _ = get_account_store().register("solo@york.ca", "password123")
    client = TestClient(create_app())

    # public page: no admin sign-in redirect (S7 lands the HTML; 404 here is the file, not the gate)
    r = client.get("/repudiate", follow_redirects=False)
    assert r.status_code in (200, 404)

    client.cookies.set("rm_session", token)
    r = client.request("DELETE", "/api/account",
                       json={"confirm_email": "solo@york.ca", "password": "wrong"})
    assert r.status_code == 403        # the ROUTE answered, not the admin gate
    r = client.request("DELETE", "/api/account",
                       json={"confirm_email": "solo@york.ca", "password": "password123"})
    assert r.status_code == 200 and r.json()["erased"] is True


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/coordinator/mentorship-stats", None),
    ("get", "/api/coordinator/alumni", None),
    ("get", "/api/coordinator/under-networked", None),
    ("get", "/api/coordinator/mentors", None),
    ("get", "/api/coordinator/orgs", None),
    ("get", "/api/coordinator/vouches", None),
    ("get", "/api/coordinator/reports/intro-outcomes", None),
    ("post", "/api/coordinator/mentorship-offers", {"student_id": 1, "mentor_id": 2}),
    ("post", "/api/coordinator/mentor-match", None),
])
def test_coordinator_routes_reject_students_and_anonymous(api, method, path, body):
    client, t, uid, _ = api
    client.cookies.clear()
    assert getattr(client, method)(path, **({"json": body} if body else {})).status_code == 401
    _as(client, t, "studentA")
    assert getattr(client, method)(path, **({"json": body} if body else {})).status_code == 403


def test_cross_tenant_coordinator_writes_are_404_not_403(api):
    """SC-C1/SC-C2, parameterized over every coordinator mutation this router owns: a foreign id is
    ABSENT, never forbidden — a 403 would confirm the object exists in the other tenant."""
    client, t, uid, _ = api
    _make_mentor(uid["alum"])
    _as(client, t, "alum")
    client.put("/api/mentorship/profile", json={"program": "CS"})
    eid = _event(client, t, "coordA")

    _as(client, t, "coordB")   # school 2 coordinator reaching into school 1
    assert client.post(f"/api/coordinator/alumni/{uid['studentA']}/verify",
                       json={"approve": True}).status_code == 404
    assert client.post("/api/coordinator/mentorship-offers",
                       json={"student_id": uid["studentA"],
                             "mentor_id": uid["alum"]}).status_code == 404
    assert client.post(f"/api/coordinator/events/{eid}/checkins",
                       json={"user_id": uid["studentA"]}).status_code == 404
    assert client.post(f"/api/events/{eid}/checkin-code").status_code == 404
    # ... and the reads answer with their own school's (empty) view, never school 1's
    assert client.get("/api/coordinator/alumni").json()["claims"] == []
    assert client.get("/api/coordinator/mentors").json()["mentors"] == []
    assert client.get("/api/coordinator/under-networked").json()["total"] == 0
    with closing(connect()) as conn:   # the cross-tenant verify really did not land
        assert conn.execute("SELECT alumni_status FROM users WHERE id=?",
                            (uid["studentA"],)).fetchone()[0] == "none"
