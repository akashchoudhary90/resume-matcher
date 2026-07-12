-- 002_phase3.sql — events/career fairs, application-thread messaging, interview scheduling
-- (docs/IMPLEMENTATION.md Phase 3). Scoring plane only — no protected columns (CI-enforced).

CREATE TABLE IF NOT EXISTS campus_events(
    id TEXT PRIMARY KEY,
    school_id INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'fair' CHECK(kind IN ('fair','info_session','workshop')),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    location TEXT,
    starts_at REAL NOT NULL,
    ends_at REAL,
    created_by INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','cancelled')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_school ON campus_events(school_id, status, starts_at);

CREATE TABLE IF NOT EXISTS event_registrations(
    event_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,                       -- student RSVP vs employer booth, from users.role
    status TEXT NOT NULL DEFAULT 'registered' CHECK(status IN ('registered','cancelled')),
    created_at REAL NOT NULL,
    PRIMARY KEY(event_id, user_id)
);

-- Messaging is scoped to an APPLICATION thread: the applicant, the posting org's employers, and
-- coordinators. No cold outreach channel exists by design (anti-spam + privacy posture).
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,
    sender_user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    sent_at REAL NOT NULL,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_app ON messages(application_id, sent_at);

CREATE TABLE IF NOT EXISTS interview_slots(
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    proposed_by INTEGER NOT NULL,
    starts_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','accepted','declined','cancelled')),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_app ON interview_slots(application_id, starts_at);
