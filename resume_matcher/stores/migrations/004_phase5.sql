-- 004_phase5.sql — Phase-5: notifications, alumni/mentorship, affiliations, event check-ins,
-- withdrawal, repudiation queue, admin sessions, vouch invites.
-- SCORING PLANE ONLY: no protected attribute / proxy column (data_planes.PROTECTED_KEYS +
-- NETWORK_FEATURE_KEYS; enforced by tests/test_platform_db.py). Nothing here joins match_results.
-- migrate() is version-gated (schema_version) and THREAD-locked per process only; a second
-- process or a crash mid-script can re-run this file, so it is written re-run tolerant: db.py
-- applies each migration inside ONE transaction (so a killed process — mem_limit 1g, OOM is
-- live — rolls the whole file back), all CREATEs are IF NOT EXISTS, the copy-rename rebuilds
-- below are additionally RE-ENTRANT (see (1)), and no bare ALTER ADD COLUMN lives here (those
-- go through _COLUMN_UPGRADES in db.py, which is idempotent).
-- NEVER `DROP TABLE IF EXISTS <t>_v2` here: mid-rebuild the scratch holds the ONLY copy of <t>,
-- so an up-front drop turns a recoverable wedge into permanent data loss (deploy.sh takes no
-- backup and this runs against the live /data/accounts.db).

-- === (1) graph_edges rebuild — extend kind + provenance CHECKs (SQLite can't ALTER a CHECK).
-- Copy-rename dance like 003's consents rebuild; all 17 columns preserved in 003's exact order;
-- all three indexes recreated below. NEW kinds: peer_coattendance (C3), classmate/org_comember
-- (C6), mentorship (C4). NEW provenance: affiliation (C6).
-- Re-entrancy: the scratch is IF NOT EXISTS, and if a DB is already wedged in the old crash
-- window (original dropped, rows only in _v2) the CTAS below re-creates the original EMPTY, the
-- copy is then a 0-row no-op and the RENAME hands the surviving rows back. Both statements are
-- no-ops on the normal path.
CREATE TABLE IF NOT EXISTS graph_edges_v2(
    id            TEXT PRIMARY KEY,
    school_id     INTEGER NOT NULL,
    edge_key      TEXT NOT NULL,
    user_a        INTEGER NOT NULL,
    user_b        INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK(kind IN (
                    'verified_vouch','self_vouch','interview','message_thread',
                    'application','event_coattendance','alumni_bridge','linkedin_connection',
                    'peer_coattendance','classmate','org_comember','mentorship')),
    weight        REAL NOT NULL DEFAULT 1.0,
    observation_count INTEGER NOT NULL DEFAULT 1,
    last_seen_at  REAL NOT NULL,
    provenance    TEXT NOT NULL CHECK(provenance IN
                    ('native','self_upload','alumni','vouch','affiliation')),
    provenance_ref TEXT,
    consent_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(consent_state IN ('pending','shareable','revoked')),
    owner_user_id INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    revoked_at    REAL,
    expires_at    REAL
);
CREATE TABLE IF NOT EXISTS graph_edges AS SELECT * FROM graph_edges_v2 WHERE 0;  -- crash recovery
INSERT INTO graph_edges_v2 SELECT * FROM graph_edges;
DROP TABLE graph_edges;
ALTER TABLE graph_edges_v2 RENAME TO graph_edges;
CREATE UNIQUE INDEX IF NOT EXISTS idx_gedges_key ON graph_edges(edge_key);
CREATE INDEX IF NOT EXISTS idx_gedges_a ON graph_edges(user_a, school_id, consent_state);
CREATE INDEX IF NOT EXISTS idx_gedges_b ON graph_edges(user_b, school_id, consent_state);

-- === (2) applications rebuild — add 'withdrawn' (B7). Copy-rename; inline UNIQUE + the one
-- explicit index recreated.
CREATE TABLE IF NOT EXISTS applications_v2(
    id TEXT PRIMARY KEY,
    posting_id TEXT NOT NULL,
    student_id INTEGER NOT NULL,
    resume_id TEXT,
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied','shortlisted','advanced','rejected','hired','withdrawn')),
    human_review_requested INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(posting_id, student_id)
);
CREATE TABLE IF NOT EXISTS applications AS SELECT * FROM applications_v2 WHERE 0;  -- see (1)
INSERT INTO applications_v2 SELECT * FROM applications;
DROP TABLE applications;
ALTER TABLE applications_v2 RENAME TO applications;
CREATE INDEX IF NOT EXISTS idx_applications_posting ON applications(posting_id);

-- === (2b) A14 one-off cleanup: revoke interview edges minted from non-accepted slots
-- (declined/cancelled interviews inflated pathfinder rank; revoked = durable, builder
-- never un-revokes). Runs before the builder gains its status='accepted' filter. Idempotent.
-- MUST stay after BOTH rebuilds above: it reads applications, which (1)/(2) may be re-creating
-- from a _v2 scratch on a crash-recovery re-run.
UPDATE graph_edges SET consent_state='revoked', revoked_at=strftime('%s','now')
WHERE kind='interview' AND consent_state != 'revoked' AND NOT EXISTS(
    SELECT 1 FROM interview_slots i JOIN applications a ON a.id=i.application_id
    WHERE i.status='accepted'
      AND ((a.student_id=graph_edges.user_a AND i.proposed_by=graph_edges.user_b)
        OR (a.student_id=graph_edges.user_b AND i.proposed_by=graph_edges.user_a)));

-- (3) intro origin (C2) and campus_events.checkin_code (C3) are NOT added here.
-- Both are plain nullable-or-constant-default ADD COLUMNs and live in db.py _COLUMN_UPGRADES
-- (idempotent; a raw ALTER here would wedge migrate() forever on any partial re-run).

-- === (4) in-app notifications (B4-lite). NO user free text is ever stored here: title/body
-- are server-composed; entity/entity_id point at the source object. Retention-purged (§2.11).
-- Composed titles/bodies never embed user emails (erasure-hygiene test, §7 S1).
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    school_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'message','interview_proposed','interview_cancelled','intro_request','intro_accepted',
        'vouch_received','application_status','posting_approved','posting_rejected',
        'mentorship_offer','mentorship_accepted','affiliation_confirmed','bridge_created',
        'vouch_contested','repudiation_notice')),
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    entity TEXT, entity_id TEXT,
    created_at REAL NOT NULL,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read_at, created_at);

-- === (5) mentorship (C4). The mentor_profiles ROW is the mentorship opt-in record (D1):
-- deleting it is the revoke. Matching additionally requires warm_intro consent live at offer time.
CREATE TABLE IF NOT EXISTS mentor_profiles(
    user_id INTEGER PRIMARY KEY,
    school_id INTEGER NOT NULL,
    program TEXT,                      -- structural matching key only; redact_text() at ingest
    topics TEXT,                       -- <=200 chars, redact_text() at ingest
    capacity INTEGER NOT NULL DEFAULT 3,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Double-opt-in mentorship offers: matcher/coordinator proposes, MENTOR accepts, only then is a
-- 'mentorship' edge written (pending -> promote_shareable). Decline is silent to the student AND
-- invisible to coordinators (D8). One OPEN offer per pair via partial unique index; terminal
-- rows are kept, so a pair can be re-offered after the cooldown (feasibility L1).
CREATE TABLE IF NOT EXISTS mentorship_offers(
    id TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL,
    student_user_id INTEGER NOT NULL,
    mentor_user_id INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT 'matcher' CHECK(origin IN ('matcher','coordinator')),
    rationale TEXT,                    -- STRUCTURAL only ('program overlap: CS'); never self-ID
    status TEXT NOT NULL DEFAULT 'offered'
        CHECK(status IN ('offered','accepted','declined','expired')),
    created_at REAL NOT NULL,
    responded_at REAL,
    expires_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mentoffer_open
    ON mentorship_offers(student_user_id, mentor_user_id) WHERE status='offered';
CREATE INDEX IF NOT EXISTS idx_mentoffer_mentor ON mentorship_offers(mentor_user_id, status);

-- === (6) affiliations (C6, self-asserted + mutually confirmed; NO registrar dependency).
-- A claim is self-disclosure; an edge exists only along an ATTESTATION PAIR (confirmer <->
-- confirmed claimant, D15), defaults pending, traverses only under the unchanged _SHAREABLE
-- predicate. claim_role is DISPLAY-ONLY (D14).
CREATE TABLE IF NOT EXISTS affiliations(
    id TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('course_section','club')),
    label_norm TEXT NOT NULL,          -- normalized join key, e.g. 'csc369:w26'
    label_display TEXT NOT NULL,       -- HTML-escaped + CSV-neutralized at write
    term TEXT,
    created_at REAL NOT NULL,
    UNIQUE(school_id, kind, label_norm)
);
CREATE TABLE IF NOT EXISTS affiliation_claims(
    id TEXT PRIMARY KEY,               -- token_urlsafe(16): doubles as the confirm-link capability
    affiliation_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    claim_role TEXT NOT NULL DEFAULT 'member'
        CHECK(claim_role IN ('member','ta','instructor','exec')),   -- display-only (D14)
    status TEXT NOT NULL DEFAULT 'unconfirmed'
        CHECK(status IN ('unconfirmed','confirmed','removed')),
    confirmed_by INTEGER,              -- the attester; set pre-confirmation during mutual bootstrap
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(affiliation_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_affclaims_user ON affiliation_claims(user_id, status);
CREATE INDEX IF NOT EXISTS idx_affclaims_aff ON affiliation_claims(affiliation_id, status);

-- === (7) event check-ins (C3): verified presence, distinct from RSVP (event_registrations).
CREATE TABLE IF NOT EXISTS event_checkins(
    event_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    checked_in_by INTEGER NOT NULL,    -- coordinator (roster) or the user themselves (code)
    method TEXT NOT NULL DEFAULT 'roster' CHECK(method IN ('roster','code')),
    at REAL NOT NULL,
    PRIMARY KEY(event_id, user_id)
);
-- campus_events.checkin_code: via _COLUMN_UPGRADES (see (3) note above).

-- === (8) hardened non-member repudiation queue (A1). A DSR *processing record*, retention-
-- bounded: first/last/company are length-capped (<=80) and passed through redact_text() AT
-- INGEST (public->admin stored-XSS guard, security H1), held ONLY for kind='name_review' rows
-- pending coordinator review, scrubbed at decision AND at TTL expiry (privacy F6). Email is
-- stored only as sha256 (challenge flow re-supplies it); the emailed token only as sha256.
CREATE TABLE IF NOT EXISTS repudiation_requests(
    id TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('email_challenge','name_review')),
    email_hash TEXT,
    challenge_hash TEXT,
    first TEXT, last TEXT, company TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','approved','denied','expired'))
        DEFAULT 'pending',
    decided_by INTEGER,
    decided_at REAL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,          -- email_challenge: now+48h; name_review: now+30d TTL
    purge_after REAL                   -- hard-delete after decision/expiry + 30 days
);
CREATE INDEX IF NOT EXISTS idx_repud_status ON repudiation_requests(status, school_id);

-- === (9) admin sessions (A15): server-side random tokens replace the derivable HMAC cookie.
-- Only the sha256 of the token is stored; logout deletes the row; expiry enforced server-side.
-- pw_fingerprint binds every session to the CURRENT admin password: rotating RM_ADMIN_PASSWORD
-- instantly invalidates all outstanding sessions (security M5 — logout-all on rotation).
CREATE TABLE IF NOT EXISTS admin_sessions(
    token_hash TEXT PRIMARY KEY,
    pw_fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

-- === (10) vouch invite links (C7): invite-by-link, never user search (enumeration fix).
-- The link token itself is NEVER at rest: token_hash = sha256(token) (feasibility L2).
CREATE TABLE IF NOT EXISTS vouch_invites(
    token_hash TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL,
    subject_user_id INTEGER NOT NULL,
    relationship_hint TEXT CHECK(relationship_hint IN
        ('worked_together','managed_them','ta_instructor','classmate','mentored_them','other')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','used','revoked','expired')),
    used_by INTEGER,
    used_at REAL,
    vouch_id TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL           -- 30 days
);
CREATE INDEX IF NOT EXISTS idx_vinvites_subject ON vouch_invites(subject_user_id, status);

-- === (11) B6 search support index.
CREATE INDEX IF NOT EXISTS idx_postings_school_status
    ON postings(school_id, status, created_at);
