-- 001_platform.sql — Phase-1 platform schema (docs/PLATFORM.md).
-- SCORING PLANE ONLY: no protected attribute / proxy column may ever be added here
-- (stores/data_planes.py PROTECTED_KEYS; enforced by tests/test_platform_db.py).
-- Every core table carries school_id (tenancy graft — York is the only row today).

CREATE TABLE IF NOT EXISTS schools(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,               -- entity name, not a person's (allowlisted in CI test)
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
INSERT INTO schools(id, name)
    SELECT 1, 'York University'
    WHERE NOT EXISTS (SELECT 1 FROM schools WHERE id = 1);

CREATE TABLE IF NOT EXISTS orgs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,               -- entity name (allowlisted)
    website TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- Employer approval is a PER-SCHOOL LINK, not a boolean on the org (Handshake-shaped; PLATFORM.md).
CREATE TABLE IF NOT EXISTS employer_school_links(
    org_id INTEGER NOT NULL,
    school_id INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','revoked')),
    reviewed_by INTEGER,
    reviewed_at REAL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY(org_id, school_id)
);

-- users/tokens/projects match api/accounts.py; role/org_id/school_id are the platform extension.
-- (A pre-existing bare users table is upgraded in place by stores/db.py _COLUMN_UPGRADES.)
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at REAL NOT NULL,
    role TEXT NOT NULL DEFAULT 'student'
        CHECK(role IN ('student','employer','coordinator','admin')),
    org_id INTEGER,
    school_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tokens(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    n_resumes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings(
    id TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL DEFAULT 1,
    org_id INTEGER,
    created_by INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','pending_review','live','closed','rejected')),
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    location TEXT,
    work_mode TEXT,
    employment_type TEXT,
    pay_min REAL,
    pay_max REAL,
    pay_currency TEXT,
    pay_period TEXT,
    apply_deadline TEXT,                     -- ISO date
    start_date TEXT,                         -- ISO date
    min_education TEXT,
    min_years REAL,
    application_method TEXT,
    application_url TEXT,
    extraction_json TEXT,                    -- full ExtractedField draft {value, span, method, ...}
    ai_disclosure INTEGER NOT NULL DEFAULT 0, -- WFWA block appended at approval (PLATFORM.md graft)
    reviewed_by INTEGER,
    reviewed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status);

CREATE TABLE IF NOT EXISTS posting_skills(
    posting_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    bucket TEXT NOT NULL CHECK(bucket IN ('must_have','required','preferred')),
    source TEXT NOT NULL DEFAULT 'user',     -- user | ndr_ai | keyword | merged
    PRIMARY KEY(posting_id, skill_id)
);

-- Append-only approval-state transition log (compliance graft).
CREATE TABLE IF NOT EXISTS posting_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id TEXT NOT NULL,
    actor_user_id INTEGER,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posting_events_posting ON posting_events(posting_id);

-- Append-only consent grants; the match-pool filter applies BEFORE retrieval (PLATFORM.md).
CREATE TABLE IF NOT EXISTS consents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL
        CHECK(purpose IN ('resume_storage','profile_matching','self_id_audit','contact')),
    version TEXT NOT NULL DEFAULT 'v1',
    granted_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id, purpose);

CREATE TABLE IF NOT EXISTS student_profiles(
    user_id INTEGER PRIMARY KEY,
    school_id INTEGER NOT NULL DEFAULT 1,
    program TEXT,
    grad_year INTEGER,
    work_auth_simple TEXT,                   -- student SELF-assessment for display; never a feature
    visibility INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    file_blob BLOB,
    extracted_text TEXT,
    redacted_text TEXT,                      -- the only free text the matching adapter may see
    uploaded_at REAL NOT NULL,
    deleted_at REAL                          -- hard DELETE honored; this marks tombstones mid-txn
);

CREATE TABLE IF NOT EXISTS applications(
    id TEXT PRIMARY KEY,
    posting_id TEXT NOT NULL,
    student_id INTEGER NOT NULL,
    resume_id TEXT,
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK(status IN ('applied','shortlisted','advanced','rejected','hired')),
    human_review_requested INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(posting_id, student_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_posting ON applications(posting_id);

-- Boundary #4 schema-enforced: this table can only ever hold fit/readiness scores.
CREATE TABLE IF NOT EXISTS match_results(
    posting_id TEXT NOT NULL,
    student_id INTEGER NOT NULL,
    fit_score REAL NOT NULL,
    grade TEXT,
    score_kind TEXT NOT NULL DEFAULT 'fit_readiness_not_hire_probability'
        CHECK(score_kind = 'fit_readiness_not_hire_probability'),
    result_json TEXT NOT NULL,
    engine_version TEXT,
    computed_at REAL NOT NULL,
    PRIMARY KEY(posting_id, student_id)
);

-- DB-backed job queue (workers/runner.py) — retryable, restart-safe, idempotent via dedupe_key.
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','error')),
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL DEFAULT 0,
    locked_by TEXT,
    dedupe_key TEXT UNIQUE,
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, run_after);

-- Append-only generic actor/action audit log.
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id TEXT,
    at REAL NOT NULL
);
