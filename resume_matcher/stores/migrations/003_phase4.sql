-- 003_phase4.sql — Phase-4 consented relationship graph + warm-intro pathfinder.
-- SCORING PLANE ONLY: no protected attribute / proxy column (data_planes.PROTECTED_KEYS;
-- enforced by tests/test_platform_db.py). The graph is a SEPARATE surface from match_results:
-- no column here is ever a scoring feature and nothing here is joined into match_results.
-- No contact NAME is ever stored (Decision 3): there is no external_persons table.
-- migrate() is version-gated (schema_version) + process-locked, so this runs exactly once.

-- === (1) SINGLE consents rebuild — the ONE source of truth ================================
-- SQLite can't ALTER a CHECK, so rebuild once, adding all four Phase-4 purposes together. On a
-- fresh DB the copy is 0 rows; on an existing DB every prior consent row is preserved.
CREATE TABLE consents_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN (
        'resume_storage','profile_matching','self_id_audit','contact',
        'contacts_upload',      -- I may upload my OWN contacts export
        'graph_discoverable',   -- I may be a node in pathfinding / be resolved as a mutual
        'warm_intro',           -- a mutual may request a double-opt-in intro TO me
        'network_analytics')),  -- my anonymized participation in fairness/overlap aggregates
    version TEXT NOT NULL DEFAULT 'v1',
    granted_at REAL NOT NULL,
    revoked_at REAL
);
INSERT INTO consents_v2(id, user_id, purpose, version, granted_at, revoked_at)
    SELECT id, user_id, purpose, version, granted_at, revoked_at FROM consents;
DROP TABLE consents;
ALTER TABLE consents_v2 RENAME TO consents;
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id, purpose);

-- === (2) member discovery identity — TOKENS ONLY, no name column =========================
CREATE TABLE IF NOT EXISTS member_graph_identity(
    user_id        INTEGER NOT NULL,
    school_id      INTEGER NOT NULL,
    identity_token TEXT NOT NULL,        -- MAC(per-school key, canonical_identity); key-versioned
    key_version    TEXT NOT NULL,
    created_at     REAL NOT NULL,
    PRIMARY KEY(user_id, identity_token)
);
CREATE INDEX IF NOT EXISTS idx_member_ident_token ON member_graph_identity(identity_token, school_id);

-- === (3) the ONE canonical edge table ====================================================
-- Both endpoints are ALWAYS members (users.id). Imported non-member contacts never appear here.
CREATE TABLE IF NOT EXISTS graph_edges(
    id            TEXT PRIMARY KEY,
    school_id     INTEGER NOT NULL,                 -- NO DEFAULT (tenant footgun fix)
    edge_key      TEXT NOT NULL,                    -- store-computed: sorted(user_a,user_b)+kind
    user_a        INTEGER NOT NULL,
    user_b        INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK(kind IN (
                    'verified_vouch','self_vouch','interview','message_thread',
                    'application','event_coattendance','alumni_bridge','linkedin_connection')),
    weight        REAL NOT NULL DEFAULT 1.0,        -- deterministic traversal-rank; NOT p(hire)
    observation_count INTEGER NOT NULL DEFAULT 1,
    last_seen_at  REAL NOT NULL,
    provenance    TEXT NOT NULL CHECK(provenance IN ('native','self_upload','alumni','vouch')),
    provenance_ref TEXT,
    consent_state TEXT NOT NULL DEFAULT 'pending'   -- default NOT shareable (both-sides consent)
        CHECK(consent_state IN ('pending','shareable','revoked')),
    owner_user_id INTEGER,                          -- who flips pending->shareable (self_upload owner)
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    revoked_at    REAL,
    expires_at    REAL                              -- retention TTL (self_upload/derived)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gedges_key ON graph_edges(edge_key);
CREATE INDEX IF NOT EXISTS idx_gedges_a ON graph_edges(user_a, school_id, consent_state);
CREATE INDEX IF NOT EXISTS idx_gedges_b ON graph_edges(user_b, school_id, consent_state);

-- === (4) permanent suppression / tombstone list (revocation durability) ==================
CREATE TABLE IF NOT EXISTS graph_suppressions(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id     INTEGER NOT NULL,
    identity_token TEXT,                            -- suppress by token (non-member repudiation)
    user_id       INTEGER,                          -- suppress by member (deletion / opt-out)
    reason        TEXT NOT NULL CHECK(reason IN
                    ('member_deleted','member_optout','third_party_repudiation')),
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suppress_token ON graph_suppressions(identity_token, school_id);
CREATE INDEX IF NOT EXISTS idx_suppress_user ON graph_suppressions(user_id);

-- === (5) employer-owned business contacts (the ONLY named-contact path) ==================
-- Reserved for an employer adding its OWN hiring manager (PIPEDA business-contact exemption).
-- NEVER used for imported LinkedIn contacts. display_label is escaped/CSV-neutralized on write.
CREATE TABLE IF NOT EXISTS employer_contacts(
    id             TEXT PRIMARY KEY,
    school_id      INTEGER NOT NULL,
    org_id         INTEGER NOT NULL,
    display_label  TEXT,                            -- NOT `name`; business contact only; escaped
    role_title     TEXT,
    contact_user_id INTEGER,                        -- set if the manager is a platform user
    added_by       INTEGER NOT NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_empcontact_org ON employer_contacts(org_id, school_id);

-- === (6) posting -> target contact =======================================================
CREATE TABLE IF NOT EXISTS posting_contacts(
    id                TEXT PRIMARY KEY,
    school_id         INTEGER NOT NULL,
    posting_id        TEXT NOT NULL,
    contact_user_id   INTEGER,                      -- hiring manager as a member (usual: created_by)
    employer_contact_id TEXT,                       -- or a business contact (employer_contacts.id)
    relation          TEXT NOT NULL DEFAULT 'hiring_manager'
        CHECK(relation IN ('hiring_manager','recruiter','referrer','team_member')),
    added_by          INTEGER NOT NULL,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posting_contact_posting ON posting_contacts(posting_id);

-- === (7) vouches — structured, job-related evidence (NOT "culture fit") ==================
CREATE TABLE IF NOT EXISTS vouches(
    id               TEXT PRIMARY KEY,
    school_id        INTEGER NOT NULL,
    voucher_user_id  INTEGER NOT NULL,
    subject_user_id  INTEGER NOT NULL,              -- vouched-for member (always a user)
    scope            TEXT NOT NULL DEFAULT 'general'
        CHECK(scope IN ('general','posting','org')),
    posting_id       TEXT,
    org_id           INTEGER,
    relationship     TEXT CHECK(relationship IN
                       ('worked_together','managed_them','ta_instructor','classmate',
                        'mentored_them','other')),
    evidence_redacted TEXT,                          -- short free text, PII-scanned + redacted AT INGEST
    verify_level     TEXT NOT NULL DEFAULT 'self'
        CHECK(verify_level IN ('self','coordinator','alumni_verified','employer_verified')),
    verified_by_user_id INTEGER,
    verified_at      REAL,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','withdrawn','contested')),
    contested_note   TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vouches_subject ON vouches(subject_user_id, status);
CREATE INDEX IF NOT EXISTS idx_vouches_voucher ON vouches(voucher_user_id, status);
CREATE INDEX IF NOT EXISTS idx_vouches_posting ON vouches(posting_id);

-- === (8) double-opt-in intro lifecycle ===================================================
CREATE TABLE IF NOT EXISTS intro_requests(
    id             TEXT PRIMARY KEY,
    school_id      INTEGER NOT NULL,
    posting_id     TEXT NOT NULL,
    application_id TEXT NOT NULL,                    -- REQUIRED: intro only after applying (oracle fix)
    requester_user_id INTEGER NOT NULL,
    target_user_id INTEGER NOT NULL,
    broker_user_id INTEGER NOT NULL,                -- the ONE mutual asked (path.nodes[1])
    hops           INTEGER NOT NULL,
    path_score     REAL NOT NULL,
    path_json      TEXT NOT NULL,                    -- ranked path + per-edge provenance quotes
    note_redacted  TEXT,                             -- student's msg to broker; <=500 enforced; redacted
    vouch_id       TEXT,                             -- links to vouches row written on accept
    status         TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested','accepted','declined','expired')),
    created_at     REAL NOT NULL,
    responded_at   REAL,
    expires_at     REAL NOT NULL,                    -- swept to 'expired'
    purge_after    REAL,                             -- retention: hard-delete after terminal + N months
    UNIQUE(requester_user_id, posting_id)
);
CREATE INDEX IF NOT EXISTS idx_intro_broker ON intro_requests(broker_user_id, status);
CREATE INDEX IF NOT EXISTS idx_intro_posting ON intro_requests(posting_id, status);

-- Append-only lifecycle log — NO free-text PII (retention fix). Opaque IDs + status only.
CREATE TABLE IF NOT EXISTS intro_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intro_id      TEXT NOT NULL,
    actor_user_id INTEGER,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intro_events_intro ON intro_events(intro_id);

-- === (9) broker abuse controls ===========================================================
CREATE TABLE IF NOT EXISTS broker_blocks(
    broker_user_id  INTEGER NOT NULL,
    blocked_user_id INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY(broker_user_id, blocked_user_id)
);
