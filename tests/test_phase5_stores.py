"""Phase-5 slice S2: the new stores (mentorship, affiliations, vouch invites, contacts, ERM) and
the graph-store integrity fixes that back them.

The adversarial requirements pinned here are the ones a refactor would quietly undo:
  * an affiliation confirmation mints edges along ATTESTATION PAIRS only, never a clique (SH-H2);
  * an unconfirmed claimant can't read the claimant list at all (P-F1);
  * a name-path repudiation never touches an active member (P-F3);
  * challenge emails are capped independently of IP (SH-H3) and asserted fields are redacted +
    capped at ingest (SH-H1);
  * every coordinator-reachable store call is school-scoped (SC-C1/D13).
"""
from __future__ import annotations

import time
from contextlib import closing

import pytest

from resume_matcher.stores.db import connect, migrate
from resume_matcher.stores.engage import EngageError, EventStore
from resume_matcher.stores.graph import GraphError, NetworkStore
from resume_matcher.stores.phase5 import (
    AffiliationStore,
    ContactStore,
    ErmStore,
    MentorStore,
    Phase5Error,
    VouchInviteStore,
)
from resume_matcher.stores.relationships import RelationshipStore


@pytest.fixture(autouse=True)
def _platform(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_ENV", "dev")
    monkeypatch.setenv("RM_GRAPH_PEPPER", "test-pepper-xyz")
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    migrate()


def _user(conn, uid, email, *, role="student", school_id=1, org_id=None, alumni="none"):
    conn.execute(
        "INSERT INTO users(id, email, pw_hash, salt, created_at, role, school_id, org_id, "
        "alumni_status) VALUES(?,?,'h','s',?,?,?,?,?)",
        (uid, email, time.time(), role, school_id, org_id, alumni))


def _grant(conn, uid, purpose, at=None):
    conn.execute("INSERT INTO consents(user_id, purpose, granted_at) VALUES(?,?,?)",
                 (uid, purpose, at if at is not None else time.time()))


# ---- C4: mentorship ------------------------------------------------------------------------------
def _mentor_world():
    with closing(connect()) as conn:
        _user(conn, 1, "stu@york.ca")
        _user(conn, 2, "alum@york.ca", alumni="verified")
        _user(conn, 3, "claimed@york.ca", alumni="self_claimed")   # NOT verified
        _user(conn, 4, "other@else.ca", school_id=2, alumni="verified")
        for uid in (2, 3, 4):
            _grant(conn, uid, "warm_intro")
            _grant(conn, uid, "graph_discoverable")
        conn.commit()


def test_mentor_eligibility_matrix():
    _mentor_world()
    ms = MentorStore()
    for uid in (2, 3, 4):
        ms.upsert_profile(uid, 1 if uid != 4 else 2, program="CS", topics="interviews",
                          capacity=2, active=True)
    eligible = {m["user_id"] for m in ms.eligible_mentors(1)}
    assert eligible == {2}          # 3 is only self-claimed; 4 is another school
    # revoking warm_intro drops the mentor without touching their profile row
    with closing(connect()) as conn:
        conn.execute("UPDATE consents SET revoked_at=? WHERE user_id=2 AND purpose='warm_intro'",
                     (time.time(),))
        conn.commit()
    assert ms.eligible_mentors(1) == []
    assert ms.get_profile(2) is not None


def test_offer_is_school_scoped_and_pair_cooldown_hides_declines():
    _mentor_world()
    ms = MentorStore()
    ms.upsert_profile(2, 1, program="CS", topics="", capacity=3, active=True)
    with pytest.raises(Phase5Error):        # security C1: cross-school student id -> "no such user"
        ms.create_offer(school_id=1, student_user_id=4, mentor_user_id=2, origin="coordinator",
                        rationale="bridge")
    offer = ms.create_offer(school_id=1, student_user_id=1, mentor_user_id=2, origin="matcher",
                            rationale="program overlap: CS")
    with pytest.raises(Phase5Error):        # open offer
        ms.create_offer(school_id=1, student_user_id=1, mentor_user_id=2, origin="matcher",
                        rationale="x")
    assert ms.respond_offer(offer["offer_id"], 2, accept=False) == {"status": "declined"}
    # D8/privacy F9: a declined pair is on cooldown, and the refusal message is IDENTICAL to the
    # open-offer one — re-offering can't be used to probe whether the mentor declined.
    with pytest.raises(Phase5Error, match="isn't available right now"):
        ms.create_offer(school_id=1, student_user_id=1, mentor_user_id=2, origin="matcher",
                        rationale="x")


def test_offer_respond_is_mentor_only_and_accept_mints_pending_edge():
    _mentor_world()
    ms = MentorStore()
    ms.upsert_profile(2, 1, program="CS", topics="", capacity=3, active=True)
    offer = ms.create_offer(school_id=1, student_user_id=1, mentor_user_id=2,
                            origin="coordinator", rationale="bridge")["offer_id"]
    with pytest.raises(Phase5Error):        # the student cannot accept on the mentor's behalf
        ms.respond_offer(offer, 1, accept=True)
    assert ms.respond_offer(offer, 2, accept=True)["status"] == "accepted"
    with closing(connect()) as conn:
        edge = conn.execute("SELECT kind, consent_state, provenance FROM graph_edges "
                            "WHERE provenance_ref=?", (offer,)).fetchone()
    # accepted != discoverable: the student never granted graph_discoverable, so it stays pending
    assert (edge["kind"], edge["provenance"], edge["consent_state"]) == \
        ("mentorship", "alumni", "pending")


def test_mentorship_stats_are_suppressed_aggregates_only():
    _mentor_world()
    ms = MentorStore()
    ms.upsert_profile(2, 1, program="CS", topics="", capacity=9, active=True)
    ms.create_offer(school_id=1, student_user_id=1, mentor_user_id=2, origin="matcher",
                    rationale="x")
    stats = ms.mentorship_stats(1)
    assert set(stats) == {"offers_made", "accepted", "active_mentors", "min_cell"}
    assert stats["offers_made"] is None and stats["accepted"] is None   # below MIN_CELL


def test_delete_profile_is_the_opt_out():
    _mentor_world()
    ms = MentorStore()
    ms.upsert_profile(2, 1, program="CS", topics="", capacity=1, active=True)
    assert ms.delete_profile(2) is True
    assert ms.get_profile(2) is None and ms.eligible_mentors(1) == []


# ---- C6: affiliations ----------------------------------------------------------------------------
def _class_world(n=6):
    with closing(connect()) as conn:
        for uid in range(1, n + 1):
            _user(conn, uid, f"s{uid}@york.ca")
        conn.commit()


def _mutually_confirm(af, a_claim, b_claim, a_uid, b_uid):
    """Bootstrap: neither side is confirmed, so it takes BOTH directions to flip either."""
    af.confirm(b_claim, a_uid)
    af.confirm(a_claim, b_uid)


def test_affiliation_edges_fold_only_along_attestation_pairs():
    """SH-H2: two colluders on a big section gain exactly ONE edge — between themselves."""
    _class_world(12)
    af = AffiliationStore()
    claims = {uid: af.claim(user_id=uid, school_id=1, kind="course_section", label="CSC369",
                            term="W26")["claim_id"] for uid in range(1, 13)}
    # a genuine, already-established pair (3 & 4) plus ten strangers who all claimed the section
    _mutually_confirm(af, claims[3], claims[4], 3, 4)
    # the colluders
    _mutually_confirm(af, claims[1], claims[2], 1, 2)
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        pairs = {tuple(sorted((r["user_a"], r["user_b"]))) for r in conn.execute(
            "SELECT user_a, user_b FROM graph_edges WHERE kind='classmate'")}
    assert pairs == {(1, 2), (3, 4)}        # NOT a clique over the 12 confirmed claimants


def test_affiliation_edge_kind_provenance_and_ttl():
    _class_world(2)
    af = AffiliationStore()
    c1 = af.claim(user_id=1, school_id=1, kind="club", label="Robotics")["claim_id"]
    c2 = af.claim(user_id=2, school_id=1, kind="club", label="Robotics")["claim_id"]
    _mutually_confirm(af, c1, c2, 1, 2)
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        e = conn.execute("SELECT * FROM graph_edges WHERE kind='org_comember'").fetchone()
    assert e["provenance"] == "affiliation" and e["consent_state"] == "pending"
    assert e["expires_at"] > time.time()    # self-asserted data is retention-bounded


def test_confirm_rules_self_non_claimant_and_bootstrap_reciprocity():
    _class_world(3)
    af = AffiliationStore()
    c1 = af.claim(user_id=1, school_id=1, kind="course_section", label="CSC369")["claim_id"]
    c2 = af.claim(user_id=2, school_id=1, kind="course_section", label="CSC369")["claim_id"]
    with pytest.raises(Phase5Error):        # self-confirm
        af.confirm(c1, 1)
    with pytest.raises(Phase5Error):        # user 3 holds no claim on this affiliation
        af.confirm(c1, 3)
    # bootstrap: one direction alone flips NOTHING (a single account can't manufacture standing)
    assert af.confirm(c1, 2)["status"] == "unconfirmed"
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM affiliation_claims WHERE id=?",
                            (c1,)).fetchone()["status"] == "unconfirmed"
    assert af.confirm(c2, 1)["status"] == "confirmed"      # reciprocal -> both flip
    with closing(connect()) as conn:
        states = {r["id"]: r["status"] for r in
                  conn.execute("SELECT id, status FROM affiliation_claims")}
    assert states[c1] == "confirmed" and states[c2] == "confirmed"


def test_confirm_daily_cap(monkeypatch):
    monkeypatch.setenv("RM_AFFILIATION_MAX_CONFIRMS_PER_DAY", "1")
    _class_world(4)
    af = AffiliationStore()
    claims = {uid: af.claim(user_id=uid, school_id=1, kind="course_section",
                            label="CSC369")["claim_id"] for uid in (1, 2, 3)}
    af.confirm(claims[2], 1)
    with pytest.raises(Phase5Error, match="Too many confirmations"):
        af.confirm(claims[3], 1)


def test_claim_cap(monkeypatch):
    monkeypatch.setenv("RM_AFFILIATION_MAX_CLAIMS", "2")
    _class_world(1)
    af = AffiliationStore()
    af.claim(user_id=1, school_id=1, kind="club", label="A")
    af.claim(user_id=1, school_id=1, kind="club", label="B")
    with pytest.raises(Phase5Error):
        af.claim(user_id=1, school_id=1, kind="club", label="C")


def test_claimants_requires_a_confirmed_claim_and_masks_emails():
    """P-F1: the claimant list is the email-enumeration surface — an unconfirmed claimant on a big
    section must get nothing at all, not a masked list."""
    _class_world(6)
    af = AffiliationStore()
    claims = {uid: af.claim(user_id=uid, school_id=1, kind="course_section",
                            label="CSC369")["claim_id"] for uid in range(1, 7)}
    aff_id = af.mine(1)[0]["affiliation_id"]
    with pytest.raises(Phase5Error):        # viewer 1 is unconfirmed -> 404, zero visibility
        af.claimants(aff_id, 1)
    _mutually_confirm(af, claims[2], claims[3], 2, 3)
    _mutually_confirm(af, claims[4], claims[5], 4, 5)
    seen = af.claimants(aff_id, 2)
    by_role = {c["claim_id"]: c["email_masked"] for c in seen}
    assert claims[1] not in by_role         # unconfirmed claimants are never listed
    # 3 attested 2 -> full address inside the pair; 4/5 are strangers -> masked to first char +
    # domain, which makes them indistinguishable from each other (that is the point)
    assert by_role[claims[3]] == "s3@york.ca"
    assert by_role[claims[4]] == by_role[claims[5]] == "s***@york.ca"


def test_claim_role_is_display_only():
    """P-F4/D14: an 'instructor' claim is self-asserted text. It must confer nothing."""
    _class_world(2)
    af = AffiliationStore()
    c1 = af.claim(user_id=1, school_id=1, kind="course_section", label="CSC369",
                  claim_role="instructor")
    c2 = af.claim(user_id=2, school_id=1, kind="course_section", label="CSC369")["claim_id"]
    _mutually_confirm(af, c1["claim_id"], c2, 1, 2)
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM graph_edges")}
    assert kinds == {"classmate"}           # NOT verified_vouch / no tier of any sort
    assert "suggested_tier" not in c1 and "has_confirmed_role" not in c1


def test_remove_claim_hard_deletes_derived_edges():
    _class_world(2)
    af = AffiliationStore()
    c1 = af.claim(user_id=1, school_id=1, kind="course_section", label="CSC369")["claim_id"]
    c2 = af.claim(user_id=2, school_id=1, kind="course_section", label="CSC369")["claim_id"]
    _mutually_confirm(af, c1, c2, 1, 2)
    RelationshipStore().build_native_edges(1)
    assert af.remove_claim(c1, 1) is True
    assert af.remove_claim(c1, 1) is False          # gone; and never someone else's claim
    with closing(connect()) as conn:
        # hard-deleted, NOT revoked (feasibility L3) — a genuine re-claim + re-confirm re-mints
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='classmate'")\
            .fetchone()[0] == 0
        # user 2 keeps their own confirmed standing: withdrawing my claim is not a way to strip
        # someone else's (and the fold still won't re-mint the edge — my claim is gone)
        assert conn.execute("SELECT status FROM affiliation_claims WHERE id=?",
                            (c2,)).fetchone()["status"] == "confirmed"
    RelationshipStore().build_native_edges(1)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='classmate'")\
            .fetchone()[0] == 0


def test_affiliation_label_is_neutralized_at_write():
    _class_world(1)
    af = AffiliationStore()
    af.claim(user_id=1, school_id=1, kind="club", label="=cmd()<script>x</script>")
    with closing(connect()) as conn:
        label = conn.execute("SELECT label_display FROM affiliations").fetchone()[0]
    assert label.startswith("'=") and "<script>" not in label


# ---- C7: vouch invites ---------------------------------------------------------------------------
def test_invite_token_is_never_at_rest_and_cross_school_is_rejected():
    with closing(connect()) as conn:
        _user(conn, 1, "subject@york.ca")
        _user(conn, 2, "voucher@york.ca")
        _user(conn, 3, "outsider@else.ca", school_id=2)
        conn.commit()
    vi = VouchInviteStore()
    inv = vi.create(subject_user_id=1, school_id=1, relationship_hint="classmate")
    token = inv["invite_token"]
    with closing(connect()) as conn:
        stored = conn.execute("SELECT token_hash FROM vouch_invites").fetchone()[0]
    assert token not in stored and len(stored) == 64          # sha256 only (feasibility L2)
    assert vi.get_open(token, 1)["subject_email"] == "subject@york.ca"
    assert vi.get_open(token, 2) is None                      # cross-school lookup: absent (M1)
    with pytest.raises(Phase5Error):                          # cross-school consume: rejected
        vi.consume(token, 3, 2, "v1")
    vi.consume(token, 2, 1, "v1")
    assert vi.get_open(token, 1) is None                      # single use
    with pytest.raises(Phase5Error):
        vi.consume(token, 2, 1, "v2")


def test_invite_cap_and_revoke():
    with closing(connect()) as conn:
        _user(conn, 1, "subject@york.ca")
        conn.commit()
    vi = VouchInviteStore()
    tokens = [vi.create(subject_user_id=1, school_id=1, relationship_hint=None)["invite_token"]
              for _ in range(10)]
    with pytest.raises(Phase5Error):
        vi.create(subject_user_id=1, school_id=1, relationship_hint=None)
    assert vi.revoke(tokens[0], 1) is True
    assert vi.revoke(tokens[1], 2) is False       # only the subject revokes their own ask
    assert len(vi.open_for_subject(1)) == 9


def test_invite_sweep_expires_open_links():
    with closing(connect()) as conn:
        _user(conn, 1, "subject@york.ca")
        conn.commit()
    vi = VouchInviteStore()
    vi.create(subject_user_id=1, school_id=1, relationship_hint=None)
    with closing(connect()) as conn:
        conn.execute("UPDATE vouch_invites SET expires_at=?", (time.time() - 1,))
        conn.commit()
    assert vi.sweep_expired() == 1


# ---- C5: contacts + ERM --------------------------------------------------------------------------
def _org_world():
    with closing(connect()) as conn:
        conn.execute("INSERT INTO orgs(id, name, created_at) VALUES(1,'Acme',?)", (time.time(),))
        conn.execute("INSERT INTO orgs(id, name, created_at) VALUES(2,'Globex',?)", (time.time(),))
        conn.execute("INSERT INTO employer_school_links(org_id, school_id, status, created_at) "
                     "VALUES(1,1,'approved',?)", (time.time(),))
        _user(conn, 10, "hr@acme.com", role="employer", org_id=1)
        _user(conn, 11, "mgr@acme.com", role="employer", org_id=1)
        _user(conn, 20, "hr@globex.com", role="employer", org_id=2)
        conn.commit()


def test_contact_free_text_is_redacted_and_neutralized_at_write():
    """P-F5: an employer contact is the one named-third-party surface; contact PII must not survive
    into a field the erasure/repudiation machinery can't reach."""
    _org_world()
    cs = ContactStore()
    c = cs.add_contact(org_id=1, school_id=1, added_by=10,
                       display_label="=Jane Doe jane@acme.com 416-555-0100",
                       role_title="Head of <b>Eng</b>", contact_user_id=None)
    with closing(connect()) as conn:
        row = conn.execute("SELECT display_label, role_title FROM employer_contacts "
                           "WHERE id=?", (c["contact_id"],)).fetchone()
    assert "jane@acme.com" not in row["display_label"] and "416-555" not in row["display_label"]
    assert row["display_label"].startswith("'=")          # CSV formula neutralized
    assert "<b>" not in row["role_title"]


def test_contact_user_must_be_an_own_org_member():
    _org_world()
    cs = ContactStore()
    with pytest.raises(Phase5Error):        # security M4: another org's user
        cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Rival",
                       role_title="HM", contact_user_id=20)
    with pytest.raises(Phase5Error):        # a user id that doesn't exist at all
        cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Ghost",
                       role_title="HM", contact_user_id=999)
    assert cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Real Manager",
                          role_title="HM", contact_user_id=11)["contact_id"]


def _posting(conn, pid="p1", org_id=1, school_id=1, created_by=10, status="live"):
    conn.execute(
        "INSERT INTO postings(id, org_id, school_id, created_by, title, description, status, "
        "created_at, updated_at) VALUES(?,?,?,?,'Dev','Python.',?,?,?)",
        (pid, org_id, school_id, created_by, status, time.time(), time.time()))


def test_delete_contact_cascades_posting_contacts():
    _org_world()
    with closing(connect()) as conn:
        _posting(conn)
        conn.commit()
    cs = ContactStore()
    cid = cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Real Manager",
                         role_title="HM", contact_user_id=11)["contact_id"]
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, employer_contact_id=cid)
    assert cs.delete_contact(cid, org_id=2) is False        # not your org
    assert cs.delete_contact(cid, org_id=1) is True
    with closing(connect()) as conn:                        # THE C5 deletion path: cascade
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts").fetchone()[0] == 0


def test_set_posting_contact_replaces_and_checks_org():
    _org_world()
    with closing(connect()) as conn:
        _posting(conn)
        conn.commit()
    cs = ContactStore()
    with pytest.raises(Phase5Error):        # security M4 on the posting path too
        cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, contact_user_id=20)
    with pytest.raises(Phase5Error):        # exactly one target
        cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10)
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, contact_user_id=11)
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, contact_user_id=10)
    with closing(connect()) as conn:
        rows = conn.execute("SELECT contact_user_id FROM posting_contacts").fetchall()
    assert len(rows) == 1 and rows[0]["contact_user_id"] == 10
    assert cs.clear_posting_contact("p1") is True


def test_contacts_for_user_is_the_erasure_hook():
    _org_world()
    with closing(connect()) as conn:
        _posting(conn)
        conn.commit()
    cs = ContactStore()
    cid = cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Real Manager",
                         role_title="HM", contact_user_id=11)["contact_id"]
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, employer_contact_id=cid)
    assert cs.contacts_for_user(11) == 1
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM employer_contacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts").fetchone()[0] == 0


def test_erm_rollup_is_org_level_only():
    _org_world()
    with closing(connect()) as conn:
        _posting(conn)
        _user(conn, 30, "stu@york.ca")
        conn.execute("INSERT INTO applications(id, posting_id, student_id, status, created_at, "
                     "updated_at) VALUES('a1','p1',30,'hired',?,?)", (time.time(), time.time()))
        conn.commit()
    rows = ErmStore().org_engagement(1)
    assert [r["name"] for r in rows] == ["Acme"]        # Globex has no link to this school
    acme = rows[0]
    assert acme["postings_live"] == 1 and acme["applications"] == 1 and acme["hires"] == 1
    assert acme["link_status"] == "approved"
    assert "student_id" not in acme and "email" not in acme


# ---- C3: check-ins (store half) -------------------------------------------------------------------
def _event_world():
    with closing(connect()) as conn:
        _user(conn, 1, "s1@york.ca")
        _user(conn, 2, "s2@york.ca")
        _user(conn, 3, "s3@else.ca", school_id=2)
        conn.commit()
    ev = EventStore()
    eid = ev.create(created_by=1, school_id=1, title="Fair", starts_at=time.time())
    ev.set_status(eid, "published")
    return ev, eid


def test_checkin_by_code_needs_registration_and_the_right_code():
    ev, eid = _event_world()
    code = ev.set_checkin_code(eid, 1)
    with pytest.raises(EngageError):        # not registered: neutral message, no code oracle
        ev.checkin_by_code(eid, 1, code)
    ev.register(eid, 1, "student")
    with pytest.raises(EngageError):
        ev.checkin_by_code(eid, 1, "wrong-code")
    ev.checkin_by_code(eid, 1, code)
    ev.checkin_by_code(eid, 1, code)        # idempotent
    rows = ev.checkins(eid, 1)
    assert len(rows) == 1 and rows[0]["method"] == "code" and rows[0]["email"] == "s1@york.ca"
    # regenerating the code invalidates the old one
    ev.set_checkin_code(eid, 1)
    ev.register(eid, 2, "student")
    with pytest.raises(EngageError):
        ev.checkin_by_code(eid, 2, code)


def test_checkin_roster_is_school_scoped():
    ev, eid = _event_world()
    with pytest.raises(EngageError):        # security M3: target user in another school
        ev.checkin_roster(eid, 3, coordinator_id=9, school_id=1)
    with pytest.raises(EngageError):        # event not in the coordinator's school
        ev.checkin_roster(eid, 1, coordinator_id=9, school_id=2)
    ev.checkin_roster(eid, 1, coordinator_id=9, school_id=1)
    assert ev.checkins(eid, 1)[0]["method"] == "roster"
    assert ev.checkins(eid, 2) == []        # cross-school read: absent


def test_set_checkin_code_cross_school_is_absent():
    ev, eid = _event_world()
    with pytest.raises(EngageError):
        ev.set_checkin_code(eid, 2)


# ---- A1: repudiation queue (store half) ----------------------------------------------------------
def test_email_challenge_caps_are_ip_independent(monkeypatch):
    """SH-H3: the route's limiter keys on a spoofable X-Forwarded-For hop, so the real backstop
    lives here. A capped call returns the SAME shape with no token — no oracle."""
    monkeypatch.setenv("RM_REPUDIATE_MAX_PER_EMAIL", "2")
    ns = NetworkStore()
    for _ in range(2):
        assert ns.create_repudiation(1, kind="email_challenge",
                                     email="x@ext.com")["email_token"]
    capped = ns.create_repudiation(1, kind="email_challenge", email="x@ext.com")
    assert capped["email_token"] is None and capped["request_id"]      # same shape, silent
    assert ns.create_repudiation(1, kind="email_challenge", email="y@ext.com")["email_token"]
    monkeypatch.setenv("RM_REPUDIATE_MAX_EMAILS_PER_DAY", "3")
    assert ns.create_repudiation(1, kind="email_challenge", email="z@ext.com")["email_token"] \
        is None
    with closing(connect()) as conn:        # a capped request writes no row at all
        assert conn.execute("SELECT COUNT(*) FROM repudiation_requests").fetchone()[0] == 3


def test_name_review_fields_are_capped_and_redacted_at_ingest():
    """SH-H1: this text is written by an anonymous public caller and rendered in an admin card."""
    ns = NetworkStore()
    rid = ns.create_repudiation(1, kind="name_review", first="Jane", last="Doe",
                                company="Acme call me at 416-555-0100 " + "x" * 200)["request_id"]
    with closing(connect()) as conn:
        row = conn.execute("SELECT first, last, company FROM repudiation_requests WHERE id=?",
                           (rid,)).fetchone()
    assert "416-555" not in row["company"] and len(row["company"]) <= 80
    with pytest.raises(GraphError):         # first+last are the minimum identifiable assertion
        ns.create_repudiation(1, kind="name_review", first="Jane")


def test_email_path_deletes_self_upload_edges_only():
    with closing(connect()) as conn:
        _user(conn, 1, "member@york.ca")
        _user(conn, 2, "peer@york.ca")
        conn.commit()
    ns = NetworkStore()
    ns.register_identity(1, 1, email="member@york.ca")
    rel = RelationshipStore()
    with closing(connect()) as conn:
        rel.upsert_edge(conn, 1, 1, 2, "linkedin_connection", provenance="self_upload")
        rel.upsert_edge(conn, 1, 1, 2, "application", provenance="native")
        conn.commit()
    made = ns.create_repudiation(1, kind="email_challenge", email="member@york.ca")
    ns.confirm_repudiation(made["request_id"], "member@york.ca", made["email_token"])
    with closing(connect()) as conn:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM graph_edges")}
        supp = conn.execute("SELECT COUNT(*) FROM graph_suppressions "
                            "WHERE identity_token IS NOT NULL").fetchone()[0]
        idents = conn.execute("SELECT COUNT(*) FROM member_graph_identity").fetchone()[0]
    assert kinds == {"application"}     # native activity survives an email challenge
    assert supp == 1 and idents == 0


def test_confirm_requires_both_email_and_token_and_scrubs():
    ns = NetworkStore()
    made = ns.create_repudiation(1, kind="email_challenge", email="x@ext.com")
    with pytest.raises(GraphError):
        ns.confirm_repudiation(made["request_id"], "x@ext.com", "wrong-token")
    with pytest.raises(GraphError):
        ns.confirm_repudiation(made["request_id"], "other@ext.com", made["email_token"])
    ns.confirm_repudiation(made["request_id"], "x@ext.com", made["email_token"])
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM repudiation_requests WHERE id=?",
                           (made["request_id"],)).fetchone()
    assert row["status"] == "confirmed" and row["email_hash"] is None
    assert row["challenge_hash"] is None and row["purge_after"] > time.time()
    with pytest.raises(GraphError):     # single use
        ns.confirm_repudiation(made["request_id"], "x@ext.com", made["email_token"])


def test_expired_challenge_is_refused():
    ns = NetworkStore()
    made = ns.create_repudiation(1, kind="email_challenge", email="x@ext.com")
    with closing(connect()) as conn:
        conn.execute("UPDATE repudiation_requests SET expires_at=?", (time.time() - 1,))
        conn.commit()
    with pytest.raises(GraphError):
        ns.confirm_repudiation(made["request_id"], "x@ext.com", made["email_token"])


def test_name_path_never_touches_an_active_member():
    """P-F3 (hard requirement): a name assertion is not proof. A matched member keeps everything
    and gets told; no member-scoped suppression is ever written on a stranger's say-so."""
    with closing(connect()) as conn:
        _user(conn, 1, "jane@york.ca")
        _user(conn, 2, "peer@york.ca")
        conn.commit()
    ns = NetworkStore()
    ns.register_identity(1, 1, first="Jane", last="Doe", company="Acme")
    rel = RelationshipStore()
    with closing(connect()) as conn:
        rel.upsert_edge(conn, 1, 1, 2, "linkedin_connection", provenance="self_upload")
        conn.commit()
    rid = ns.create_repudiation(1, kind="name_review", first="Jane", last="Doe",
                                company="Acme")["request_id"]
    preview = ns.list_repudiations(1)
    assert preview[0]["member_matched"] is True and preview[0]["contact_matches"] == 0
    out = ns.decide_repudiation(1, rid, coordinator_id=9, approve=True)
    assert out["member_matched"] is True and out["contacts_deleted"] == 0
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0] == 1   # untouched
        assert conn.execute("SELECT COUNT(*) FROM member_graph_identity").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM graph_suppressions").fetchone()[0] == 0
        # instead: the member is told, and pointed at their own controls
        note = conn.execute("SELECT user_id, kind FROM notifications").fetchone()
        row = conn.execute("SELECT status, first FROM repudiation_requests WHERE id=?",
                           (rid,)).fetchone()
    assert (note["user_id"], note["kind"]) == (1, "repudiation_notice")
    assert row["status"] == "approved" and row["first"] is None      # asserted fields scrubbed


def test_name_path_deletes_matching_non_member_contact_rows():
    """privacy F5 match path: a non-member named in an employer's contact list can get out."""
    _org_world()
    ns = NetworkStore()
    cs = ContactStore()
    cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Jane Doe",
                   role_title="Head of Eng", contact_user_id=None)
    with closing(connect()) as conn:
        _posting(conn)
        conn.commit()
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10,
                           employer_contact_id=cs.list_contacts(1)[0]["contact_id"])
    rid = ns.create_repudiation(1, kind="name_review", first="Jane", last="Doe",
                                company="Acme")["request_id"]
    assert ns.list_repudiations(1)[0]["contact_matches"] == 1
    out = ns.decide_repudiation(1, rid, coordinator_id=9, approve=True)
    assert out == {"ok": True, "status": "approved", "member_matched": False,
                   "contacts_deleted": 1}
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM employer_contacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM graph_suppressions").fetchone()[0] == 1


def test_name_path_spares_a_member_who_never_opted_into_discovery():
    """P-F3, the hole the mgi-only member test left: member_graph_identity is written ONLY by
    register_identity, which needs graph_discoverable consent. An employer's hiring manager
    typically never opts in, so they have zero mgi rows — and the name path then classified them as
    a non-member and hard-deleted their live records on an unauthenticated stranger's say-so, with
    no notice to them and a queue preview telling the coordinator no member was involved."""
    _org_world()
    ns, cs = NetworkStore(), ContactStore()
    cid = cs.add_contact(org_id=1, school_id=1, added_by=10, display_label="Jane Doe",
                         role_title="Head of Eng", contact_user_id=11)["contact_id"]
    with closing(connect()) as conn:
        _posting(conn)
        conn.commit()
    cs.set_posting_contact(posting_id="p1", school_id=1, added_by=10, employer_contact_id=cid)
    with closing(connect()) as conn:        # THE trigger: user 11 is a member with no mgi row
        assert conn.execute("SELECT COUNT(*) FROM member_graph_identity").fetchone()[0] == 0
    rid = ns.create_repudiation(1, kind="name_review", first="Jane", last="Doe",
                                company="Acme")["request_id"]
    preview = ns.list_repudiations(1)[0]
    # the coordinator must not be told "no member involved" about a member's own record
    assert preview["member_matched"] is True and preview["contact_matches"] == 0
    out = ns.decide_repudiation(1, rid, coordinator_id=9, approve=True)
    assert out["member_matched"] is True and out["contacts_deleted"] == 0
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM employer_contacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM posting_contacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM graph_suppressions").fetchone()[0] == 0
        note = conn.execute("SELECT user_id, kind FROM notifications").fetchone()
    assert (note["user_id"], note["kind"]) == (11, "repudiation_notice")


def test_name_review_queue_is_bounded_and_dedupes(monkeypatch):
    """The name path had no cap at all: an anonymous caller could grow the pending queue without
    bound, and every pending row costs the coordinator a tokenizing employer_contacts pass."""
    monkeypatch.setenv("RM_REPUDIATE_MAX_PENDING_REVIEWS", "2")
    ns = NetworkStore()
    assert ns.create_repudiation(1, kind="name_review", first="Ann", last="One")["request_id"]
    ns.create_repudiation(1, kind="name_review", first="Ann", last="One")     # dup -> collapses
    ns.create_repudiation(1, kind="name_review", first="Bea", last="Two")
    capped = ns.create_repudiation(1, kind="name_review", first="Cal", last="Three")
    assert capped["request_id"] and capped["email_token"] is None             # same shape, silent
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM repudiation_requests").fetchone()[0] == 2
    # per-school: school 1 filling its queue must not deny school 2's non-member DSR right
    assert ns.create_repudiation(2, kind="name_review", first="Cal", last="Three")["request_id"]
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM repudiation_requests WHERE school_id=2")\
            .fetchone()[0] == 1


def test_email_challenge_global_cap_is_per_school(monkeypatch):
    """The 50/day cap counted email_challenge rows across the WHOLE deployment, so one school's
    traffic silently denied every other school's non-member DSR right — a legal right, and silent
    denial is the bad failure mode."""
    monkeypatch.setenv("RM_REPUDIATE_MAX_EMAILS_PER_DAY", "2")
    ns = NetworkStore()
    for addr in ("a@ext.com", "b@ext.com"):
        assert ns.create_repudiation(1, kind="email_challenge", email=addr)["email_token"]
    assert ns.create_repudiation(1, kind="email_challenge",
                                 email="c@ext.com")["email_token"] is None   # school 1 exhausted
    assert ns.create_repudiation(2, kind="email_challenge", email="c@ext.com")["email_token"]


def test_decide_is_school_scoped_and_denial_scrubs_without_executing():
    _org_world()
    ns = NetworkStore()
    ContactStore().add_contact(org_id=1, school_id=1, added_by=10, display_label="Jane Doe",
                               role_title="HM", contact_user_id=None)
    rid = ns.create_repudiation(1, kind="name_review", first="Jane", last="Doe",
                                company="Acme")["request_id"]
    with pytest.raises(GraphError):         # security C1: another tenant's coordinator
        ns.decide_repudiation(2, rid, coordinator_id=9, approve=True)
    assert ns.list_repudiations(2) == []
    out = ns.decide_repudiation(1, rid, coordinator_id=9, approve=False)
    assert out["status"] == "denied" and out["contacts_deleted"] == 0
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM repudiation_requests WHERE id=?", (rid,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM employer_contacts").fetchone()[0] == 1
    assert row["first"] is None and row["decided_by"] == 9   # scrubbed either way (privacy F6)


def test_executors_are_unreachable_from_any_route():
    """SL-L2: the challenge/queue IS the authorization — no route may call an executor, and the
    old instant public `repudiate()` must not exist for anyone to reach."""
    import subprocess
    assert not hasattr(NetworkStore, "repudiate")
    hits = subprocess.run(
        ["git", "grep", "-n", "repudiate_execute", "--", "resume_matcher/api"],
        capture_output=True, text=True, cwd=".").stdout
    assert hits == ""
