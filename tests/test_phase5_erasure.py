"""Phase-5 A3: the two-plane account-erasure cascade (stores/erasure.py, PHASE5.md §6).

The contract under test: people data is hard-deleted, append-only logs are ANONYMIZED (never
deleted), employer business records survive their author behind a sentinel, the tombstone lands
inside the same transaction as the users row, a dry run touches nothing, and a second run is a
no-op — because erasure is retried after crashes, not rolled back.
"""
from __future__ import annotations

import json
import time
from contextlib import closing

import pytest

from resume_matcher.stores.audit_store import AuditDB
from resume_matcher.stores.db import connect, migrate
from resume_matcher.stores.erasure import ErasureError, erase_account

# Every table the cascade claims to clear of the erased user, with the column(s) that would still
# name them. Parameterized so a future table added to _plan without a test trips this sweep.
_NO_RESIDUE = [
    ("notifications", "user_id=?"),
    ("intro_requests", "requester_user_id=? OR target_user_id=? OR broker_user_id=?"),
    ("vouches", "voucher_user_id=? OR subject_user_id=?"),
    ("vouch_invites", "subject_user_id=? OR used_by=?"),
    ("graph_edges", "user_a=? OR user_b=?"),
    ("member_graph_identity", "user_id=?"),
    ("broker_blocks", "broker_user_id=? OR blocked_user_id=?"),
    ("mentor_profiles", "user_id=?"),
    ("mentorship_offers", "student_user_id=? OR mentor_user_id=?"),
    ("affiliation_claims", "user_id=? OR confirmed_by=?"),
    ("event_checkins", "user_id=?"),
    ("event_registrations", "user_id=?"),
    ("messages", "sender_user_id=?"),
    ("interview_slots", "proposed_by=?"),
    ("applications", "student_id=?"),
    ("match_results", "student_id=?"),
    ("resumes", "user_id=?"),
    ("student_profiles", "user_id=?"),
    ("projects", "user_id=?"),
    ("posting_contacts", "contact_user_id=? OR added_by=?"),
    ("employer_contacts", "contact_user_id=? OR added_by=?"),
    ("repudiation_requests", "decided_by=?"),
    ("jobs", "owner_user_id=?"),
    ("consents", "user_id=?"),
    ("events", "actor_user_id=?"),
    ("posting_events", "actor_user_id=?"),
    ("intro_events", "actor_user_id=?"),
    ("users", "id=?"),
]

VICTIM = 7          # the student being erased
PEER = 8            # a surviving member entangled with them
EMPLOYER = 9        # the erased employer in the business-records test


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("RM_ACCOUNTS_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("RM_AUDIT_DB", str(tmp_path / "audit.db"))
    migrate()


def _user(conn, uid: int, email: str, role: str = "student", org_id=None) -> None:
    conn.execute("INSERT INTO users(id,email,pw_hash,salt,created_at,role,org_id,school_id) "
                 "VALUES(?,?,'h','s',0,?,?,1)", (uid, email, role, org_id))


def _seed_entangled_student() -> None:
    """One row in EVERY table the cascade touches, plus surviving rows that must NOT be collateral."""
    now = time.time()
    with closing(connect()) as conn:
        _user(conn, VICTIM, "victim@york.ca")
        _user(conn, PEER, "peer@york.ca")
        conn.execute("INSERT INTO tokens(token_hash,user_id,created_at) VALUES('th',?,?)",
                     (VICTIM, now))
        conn.execute("INSERT INTO notifications(user_id,school_id,kind,title,created_at) "
                     "VALUES(?,1,'message','hi',?)", (VICTIM, now))
        conn.execute("INSERT INTO notifications(user_id,school_id,kind,title,created_at) "
                     "VALUES(?,1,'message','hi',?)", (PEER, now))          # survives
        # an intro where the victim is a principal, and one where they are only an INTERIOR hop
        for iid, posting, requester, broker, target, nodes in (
            ("i-principal", "p1", VICTIM, PEER, 3, [VICTIM, PEER, 3]),
            ("i-interior", "p2", 4, 5, 6, [4, 5, VICTIM, 6]),
            ("i-clean", "p3", 4, 5, 6, [4, 5, 6]),
        ):
            conn.execute(
                "INSERT INTO intro_requests(id,school_id,posting_id,application_id,"
                "requester_user_id,target_user_id,broker_user_id,hops,path_score,path_json,"
                "status,created_at,expires_at) VALUES(?,1,?,'a',?,?,?,2,0.5,?,'requested',?,?)",
                (iid, posting, requester, target, broker, json.dumps({"nodes": nodes}),
                 now, now + 999))
        conn.execute("INSERT INTO vouches(id,school_id,voucher_user_id,subject_user_id,"
                     "created_at,updated_at) VALUES('v1',1,?,?,?,?)", (PEER, VICTIM, now, now))
        conn.execute("INSERT INTO vouch_invites(token_hash,school_id,subject_user_id,used_by,"
                     "created_at,expires_at) VALUES('vi1',1,?,NULL,?,?)", (VICTIM, now, now + 999))
        conn.execute("INSERT INTO vouch_invites(token_hash,school_id,subject_user_id,used_by,"
                     "created_at,expires_at) VALUES('vi2',1,?,?,?,?)", (PEER, VICTIM, now, now + 999))
        conn.execute("INSERT INTO graph_edges(id,school_id,edge_key,user_a,user_b,kind,"
                     "last_seen_at,provenance,created_at,updated_at) "
                     "VALUES('e1',1,'k1',?,?,'interview',?,'native',?,?)", (VICTIM, PEER, now, now, now))
        conn.execute("INSERT INTO member_graph_identity(user_id,school_id,identity_token,"
                     "key_version,created_at) VALUES(?,1,'tok','v1',?)", (VICTIM, now))
        conn.execute("INSERT INTO broker_blocks(broker_user_id,blocked_user_id,created_at) "
                     "VALUES(?,?,?)", (PEER, VICTIM, now))
        conn.execute("INSERT INTO mentor_profiles(user_id,school_id,capacity,created_at,updated_at)"
                     " VALUES(?,1,3,?,?)", (VICTIM, now, now))
        conn.execute("INSERT INTO mentorship_offers(id,school_id,student_user_id,mentor_user_id,"
                     "created_at,expires_at) VALUES('mo1',1,?,?,?,?)", (VICTIM, PEER, now, now + 9))
        conn.execute("INSERT INTO affiliations(id,school_id,kind,label_norm,label_display,"
                     "created_at) VALUES('af1',1,'course_section','csc369:w26','CSC369',?)", (now,))
        conn.execute("INSERT INTO affiliation_claims(id,affiliation_id,user_id,status,confirmed_by,"
                     "created_at,updated_at) VALUES('c-victim','af1',?,'confirmed',?,?,?)",
                     (VICTIM, PEER, now, now))
        # the PEER's own claim, attested BY the victim: it must survive with the link anonymized
        conn.execute("INSERT INTO affiliation_claims(id,affiliation_id,user_id,status,confirmed_by,"
                     "created_at,updated_at) VALUES('c-peer','af1',?,'confirmed',?,?,?)",
                     (PEER, VICTIM, now, now))
        conn.execute("INSERT INTO campus_events(id,school_id,title,starts_at,created_by,"
                     "created_at,updated_at) VALUES('ev1',1,'Fair',?,?,?,?)", (now, PEER, now, now))
        conn.execute("INSERT INTO event_checkins(event_id,user_id,checked_in_by,at) "
                     "VALUES('ev1',?,?,?)", (VICTIM, PEER, now))
        conn.execute("INSERT INTO event_registrations(event_id,user_id,role,created_at) "
                     "VALUES('ev1',?,'student',?)", (VICTIM, now))
        conn.execute("INSERT INTO postings(id,school_id,created_by,title,created_at,updated_at) "
                     "VALUES('p1',1,?, 'Intern',?,?)", (PEER, now, now))
        conn.execute("INSERT INTO applications(id,posting_id,student_id,created_at,updated_at) "
                     "VALUES('a1','p1',?,?,?)", (VICTIM, now, now))
        # a message sent BY the peer on the victim's application dies with the application
        conn.execute("INSERT INTO messages(application_id,sender_user_id,body,sent_at) "
                     "VALUES('a1',?,'hello',?)", (PEER, now))
        conn.execute("INSERT INTO interview_slots(id,application_id,proposed_by,starts_at,"
                     "ends_at,created_at) VALUES('s1','a1',?,?,?,?)", (PEER, now, now + 1, now))
        conn.execute("INSERT INTO match_results(posting_id,student_id,fit_score,result_json,"
                     "computed_at) VALUES('p1',?,0.5,'{}',?)", (VICTIM, now))
        conn.execute("INSERT INTO resumes(id,user_id,filename,file_blob,extracted_text,"
                     "uploaded_at) VALUES('r1',?,'cv.pdf',X'00','secret text',?)", (VICTIM, now))
        conn.execute("INSERT INTO student_profiles(user_id,school_id,program,updated_at) "
                     "VALUES(?,1,'CS',?)", (VICTIM, now))
        conn.execute("INSERT INTO projects(id,user_id,name,mode,created_at,payload) "
                     "VALUES('pr1',?,'n','single',?,'{}')", (VICTIM, now))
        conn.execute("INSERT INTO orgs(id,name,created_at) VALUES(1,'Acme',?)", (now,))
        conn.execute("INSERT INTO employer_contacts(id,school_id,org_id,display_label,"
                     "contact_user_id,added_by,created_at) VALUES('ec1',1,1,'HM',?,?,?)",
                     (VICTIM, VICTIM, now))
        conn.execute("INSERT INTO posting_contacts(id,school_id,posting_id,contact_user_id,"
                     "added_by,created_at) VALUES('pc1',1,'p1',?,?,?)", (VICTIM, VICTIM, now))
        conn.execute("INSERT INTO repudiation_requests(id,school_id,kind,status,decided_by,"
                     "created_at,expires_at) VALUES('rq1',1,'name_review','denied',?,?,?)",
                     (VICTIM, now, now + 9))
        conn.execute("INSERT INTO jobs(id,kind,owner_user_id,status,payload_json,result_json,"
                     "created_at) VALUES('j-live','build_edges',?,'queued','{\"a\":1}',NULL,?)",
                     (VICTIM, now))
        conn.execute("INSERT INTO jobs(id,kind,owner_user_id,status,payload_json,result_json,"
                     "created_at) VALUES('j-done','build_edges',?,'done','{\"pii\":\"x\"}',"
                     "'{\"r\":1}',?)", (VICTIM, now))
        conn.execute("INSERT INTO consents(user_id,purpose,granted_at) VALUES(?,'warm_intro',?)",
                     (VICTIM, now))
        conn.execute("INSERT INTO events(actor_user_id,action,entity,entity_id,at) "
                     "VALUES(?,'login','user',?,?)", (VICTIM, str(VICTIM), now))
        conn.execute("INSERT INTO events(actor_user_id,action,entity,entity_id,at) "
                     "VALUES(?,'alumni_verified','user',?,?)", (PEER, str(VICTIM), now))
        conn.execute("INSERT INTO posting_events(posting_id,actor_user_id,to_status,at) "
                     "VALUES('p1',?,'live',?)", (VICTIM, now))
        conn.execute("INSERT INTO intro_events(intro_id,actor_user_id,to_status,at) "
                     "VALUES('i-principal',?,'requested',?)", (VICTIM, now))
        conn.commit()
    AuditDB().set_self_id(f"student-{VICTIM}", {"gender": "woman"})


def _counts(uid: int) -> dict[str, int]:
    with closing(connect()) as conn:
        return {table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}",
            (uid,) * where.count("?")).fetchone()[0] for table, where in _NO_RESIDUE}


def test_unknown_user_refuses():
    with pytest.raises(ErasureError):
        erase_account(999)


def test_dry_run_touches_nothing():
    _seed_entangled_student()
    before = _counts(VICTIM)
    out = erase_account(VICTIM, dry_run=True)
    assert out["dry_run"] is True and out["tombstoned"] is False
    assert out["audit_plane_deleted"] is False
    assert out["tables"]["resumes"] == 1 and out["tables"]["users"] == 1
    # the interior-hop intro is counted in the preview, not just the principal-column ones
    assert out["tables"]["intro_requests"] == 2
    assert _counts(VICTIM) == before                       # nothing moved
    assert AuditDB().has_self_id(f"student-{VICTIM}") is True


@pytest.mark.parametrize("table,where", _NO_RESIDUE, ids=[t for t, _ in _NO_RESIDUE])
def test_erasure_leaves_no_residue(table, where):
    _seed_entangled_student()
    erase_account(VICTIM)
    with closing(connect()) as conn:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",
                         (VICTIM,) * where.count("?")).fetchone()[0]
    assert n == 0, f"{table} still names the erased user"


def test_tombstone_and_audit_plane_and_survivors():
    _seed_entangled_student()
    out = erase_account(VICTIM)
    assert out["tombstoned"] is True and out["audit_plane_deleted"] is True
    assert AuditDB().has_self_id(f"student-{VICTIM}") is False     # phase 1 really ran
    with closing(connect()) as conn:
        assert conn.execute("SELECT reason FROM graph_suppressions WHERE user_id=?",
                            (VICTIM,)).fetchone()["reason"] == "member_deleted"
        # collateral check: the peer keeps their own rows
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (PEER,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=?",
                            (PEER,)).fetchone()[0] == 1
        # the peer's OWN claim survives; only the attestation link to the erased user is anonymized
        peer_claim = conn.execute("SELECT * FROM affiliation_claims WHERE id='c-peer'").fetchone()
        assert peer_claim is not None and peer_claim["confirmed_by"] is None
        # a >2-hop intro merely PASSING THROUGH the erased member goes too (privacy F8)...
        assert conn.execute("SELECT COUNT(*) FROM intro_requests WHERE id='i-interior'"
                            ).fetchone()[0] == 0
        # ...while an unrelated path survives
        assert conn.execute("SELECT COUNT(*) FROM intro_requests WHERE id='i-clean'"
                            ).fetchone()[0] == 1


def test_append_only_logs_are_anonymized_not_deleted():
    _seed_entangled_student()
    erase_account(VICTIM)
    with closing(connect()) as conn:
        # the rows REMAIN (audit-retention basis) with the actor nulled...
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM posting_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM intro_events").fetchone()[0] == 1
        # ...and privacy F8: the erased user as the SUBJECT of a logged action is not re-linkable,
        # even though the actor of that row (the coordinator) survives.
        subject = conn.execute("SELECT actor_user_id, entity_id FROM events "
                               "WHERE action='alumni_verified'").fetchone()
        assert subject["actor_user_id"] == PEER and subject["entity_id"] is None


def test_finished_jobs_keep_only_anonymous_shape():
    _seed_entangled_student()
    erase_account(VICTIM)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE id='j-live'").fetchone()[0] == 0
        done = conn.execute("SELECT * FROM jobs WHERE id='j-done'").fetchone()
        assert done["owner_user_id"] is None
        assert done["payload_json"] == "{}" and done["result_json"] is None


def test_employer_postings_survive_with_the_sentinel_and_live_ones_close():
    now = time.time()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO orgs(id,name,created_at) VALUES(1,'Acme',?)", (now,))
        _user(conn, EMPLOYER, "hr@acme.com", role="employer", org_id=1)
        _user(conn, PEER, "coord@york.ca", role="coordinator")
        conn.execute("INSERT INTO postings(id,school_id,org_id,created_by,status,title,reviewed_by,"
                     "created_at,updated_at) VALUES('p-live',1,1,?,'live','Intern',?,?,?)",
                     (EMPLOYER, EMPLOYER, now, now))
        conn.execute("INSERT INTO postings(id,school_id,org_id,created_by,status,title,"
                     "created_at,updated_at) VALUES('p-closed',1,1,?,'closed','Old',?,?)",
                     (EMPLOYER, now, now))
        conn.execute("INSERT INTO employer_school_links(org_id,school_id,status,reviewed_by,"
                     "created_at) VALUES(1,1,'approved',?,?)", (EMPLOYER, now))
        conn.commit()

    erase_account(EMPLOYER)
    with closing(connect()) as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM postings")}
        assert len(rows) == 2, "org business records must outlive their author"
        assert all(r["created_by"] == 0 for r in rows.values())   # the erasure sentinel
        assert rows["p-live"]["reviewed_by"] is None
        # the org's last member is gone, so nothing live is left for nobody to answer
        assert rows["p-live"]["status"] == "closed"
        assert conn.execute("SELECT reviewed_by FROM employer_school_links").fetchone()[0] is None


def test_live_postings_stay_live_while_the_org_has_another_member():
    now = time.time()
    with closing(connect()) as conn:
        conn.execute("INSERT INTO orgs(id,name,created_at) VALUES(1,'Acme',?)", (now,))
        _user(conn, EMPLOYER, "hr@acme.com", role="employer", org_id=1)
        _user(conn, PEER, "hr2@acme.com", role="employer", org_id=1)
        conn.execute("INSERT INTO postings(id,school_id,org_id,created_by,status,title,"
                     "created_at,updated_at) VALUES('p-live',1,1,?,'live','Intern',?,?)",
                     (EMPLOYER, now, now))
        conn.commit()
    erase_account(EMPLOYER)
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM postings").fetchone()[0] == "live"


def test_second_run_is_a_noop_not_a_crash():
    """Erasure is retried after a crash, never rolled back — so the users row being gone must read
    as 'already done', and a re-run must not double-tombstone the graph."""
    _seed_entangled_student()
    erase_account(VICTIM)
    with pytest.raises(ErasureError):
        erase_account(VICTIM)
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_suppressions WHERE user_id=?",
                            (VICTIM,)).fetchone()[0] == 1
