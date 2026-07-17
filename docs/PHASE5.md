# Phase 5 — build-ready specification (FINAL)

**Scope:** all of group A (A1–A18); B3, B4-lite, B6, B7, B8, B10; all of group C (C1–C7; C6 = self-asserted mutually-confirmed variant; C5 includes the employer_contacts deletion path).
**Out of scope (documented as gates, not built):** OIDC SSO, real KMS, Postgres port, legal/process gates (FIPPA PIA, legal opinion, residency, breach runbook), Playwright, vision-PDF, multi-role picker, URL ingest.
**Everything ships behind `RM_PLATFORM_ENABLED`.** Migration is `004_phase5.sql`, version-gated by `schema_version` (`migrate()` in `stores/db.py` is thread-locked per process — NOT cross-process locked — so 004 is written re-run tolerant; see §1).

This final spec folds three adversarial reviews (privacy, security, feasibility) of the draft. Every CRITICAL/HIGH finding is a **hard requirement** — §9 lists each with its required fix, the way `docs/RELATIONSHIPS.md` §2 did for Phase 4. MED/LOW findings are applied in place; the three partially-rejected recommendations are listed at the end of §9 with reasons.

All file paths below are relative to `D:/Gitclone/Resume Matching/`.

---

## 0. Top-level decisions (binding)

| # | Decision | Rationale |
|---|---|---|
| D1 | **No consents rebuild in 004.** No new consent purpose is added. Mentorship availability (C4) is an *active opt-in record* (`mentor_profiles` row), not passive processing; being matched as a mentor additionally requires the existing `warm_intro` consent, and traversal of any mentorship edge still requires both-endpoint `graph_discoverable`. | The consents rebuild is the single most dangerous migration op (003 did it once, by design "exactly once"). Every new Phase-5 flow is either an affirmative act (its own table row is the consent record, revocable by deleting the row) or is already covered by the four Phase-4 purposes. |
| D2 | **`graph_edges` IS rebuilt** (copy-rename dance, 003-style) to extend the `kind` CHECK with `peer_coattendance`, `classmate`, `org_comember`, `mentorship`, and the `provenance` CHECK with `affiliation`. | New C3/C4/C6 edge kinds cannot reuse existing kinds without corrupting `EDGE_STRENGTH` semantics and provenance quotes. SQLite cannot ALTER a CHECK. |
| D3 | **`applications` IS rebuilt** to add `'withdrawn'` to the status CHECK (B7). Same dance; the inline `UNIQUE(posting_id, student_id)` and the one explicit index (`idx_applications_posting`) recreated. | Only way to extend the CHECK. |
| D4 | **Alumni (C4) = `ALTER TABLE users ADD COLUMN alumni_status`, applied via `_COLUMN_UPGRADES` in `stores/db.py` — NOT a users-table rebuild, NOT a new role.** `alumni_status TEXT NOT NULL DEFAULT 'none' CHECK(alumni_status IN ('none','self_claimed','verified'))`. Alumni keep `role='student'`. **`grad_year` is never collected or stored anywhere** (age proxy — privacy F2); coordinators verify against SIS by email. | The users table is the auth root; a rebuild risks the whole platform for a one-column gain. `_COLUMN_UPGRADES` is idempotent, covers legacy bare users tables, and `_ensure_columns` runs on every `migrate()`. A verified alum is an ex-student with extra *surfaces* (mentor opt-in, `alumni_verified` vouch tier), not different permissions. |
| D5 | **Intro origin (C2)** = `intro_requests.origin TEXT NOT NULL DEFAULT 'organic' CHECK(origin IN ('organic','bridged'))`, added via `_COLUMN_UPGRADES` (idempotent — feasibility M1), NOT via ALTER inside 004. `origin='bridged'` iff the chosen path contains an `alumni_bridge` or `mentorship` edge, computed at create time. | Spec'd before/after impact ratios (RELATIONSHIPS.md Slice AI) need it; `_COLUMN_UPGRADES` survives partial re-runs where a raw ALTER wedges migrate() forever. |
| D6 | **C5 needs no schema change**: `employer_contacts` + `posting_contacts` (003 §5/§6) already carry everything. Phase 5 adds the write/read/delete API + the erasure hooks. Contact free text goes through `redact_text()` at write (privacy F5). | Schema-ready since 003. |
| D7 | **New Phase-5 endpoints live in a new router `resume_matcher/api/phase5.py`**; A-item enforcement edits happen in-place in `api/platform.py` / stores. B6/B7 route changes touch existing routes and therefore go in `platform.py`. | Minimizes churn in the 1 367-line `platform.py`. |
| D8 | **Silent-decline invariant extends to notifications AND to coordinators**: no notification is ever emitted for `intro declined`, `mentorship offer declined`, or repudiation denial to the party that must not learn of it, and **no coordinator surface exposes per-offer mentorship status** (privacy F9) — coordinators see only MIN_CELL'd aggregates. A declined intro remains indistinguishable from "no path"; a declined mentorship offer is indistinguishable (to everyone but the mentor) from a pending one. | Boundary discipline from RELATIONSHIPS.md Slice AE. |
| D9 | **Peer co-attendance edges (C3) fold only from *verified check-ins* (new `event_checkins`), never from RSVPs, only for events with ≤ `RM_PEER_EDGE_MAX_CHECKINS` (default 150) student check-ins, and via ONE set-based upsert per event, skipping events with no new check-ins since the last fold** (feasibility M4). | RSVP ≠ presence; an unbounded fair produces O(n²) edge blowup; a naive per-pair loop is ~300k statements per rebuild on a busy term. |
| D10 | **C7 vouch invites are link-tokens, not user search.** There is no member-search endpoint anywhere in Phase 5; every subject-resolution is invite-by-link or an existing relationship surface. Invite tokens are stored **hashed** (sha256), never cleartext at rest (feasibility L2). | Member-enumeration oracle (C7 risk note); same at-rest discipline as A15 admin sessions. |
| D11 | **A8 snapshots + complementary suppression live in `audit_store.py` (`AuditDB` creates its own tables in audit.db)** — audit.db has no migration runner and must never appear in 004 (two-plane separation). | Boundary #2. |
| D12 | **Erasure (A3) is: audit.db first, then one platform.db transaction, tombstone inside that transaction.** Cross-DB atomicity is impossible; the chosen order makes every failure mode retry-safe (see §6). | FIPPA hard-delete requirement. |
| D13 | **Every coordinator/admin store method that reads or mutates a keyed row takes `school_id` and enforces `AND school_id=?`; routes derive `school_id` from `user["school_id"]`, never from body/query; cross-tenant references answer 404 (never 403).** | Security C1/C2 — cross-tenant coordinator access was the #1 finding of the 2026-07-12 audit; Phase 5 must not re-introduce the class. |
| D14 | **`affiliation_claims.claim_role` is display-only.** No code path may branch on it: it confers no vouch tier, no auto-flag, no authorization. The draft's TA/instructor auto-flag (`has_confirmed_role` → `suggested_tier='coordinator'`) is **removed** from Phase 5. | Privacy F4 / security H2: self-asserted + peer-bootstrapped "instructor" must not manufacture coordinator-tier authority. Removing the consumer is cheaper and safer than building coordinator attestation of claim roles. |
| D15 | **Affiliation edges (C6) fold ONLY between an attestation pair** — the confirmer and the claimant they confirmed — never as a clique over all confirmed claimants. Confirmation is directed by **confirm-links** (a claimant shares their own claim's confirm URL out-of-band); no endpoint ever lists unconfirmed claimants. | Security H2 (two colluding accounts must not gain edges to strangers) + privacy F1 (the claimant list must not be an email-harvesting oracle). |
| D16 | **Edge folds carry the *source interaction timestamp*** (`seen_at`) into `upsert_edge`; `last_seen_at` only advances when the source interaction is newer. | Feasibility H1: without this, the A13 pre-consent guard is a no-op (every rebuild bumps `last_seen_at` past any consent grant). |

---

## 1. Migration `resume_matcher/stores/migrations/004_phase5.sql` — full DDL

```sql
-- 004_phase5.sql — Phase-5: notifications, alumni/mentorship, affiliations, event check-ins,
-- withdrawal, repudiation queue, admin sessions, vouch invites.
-- SCORING PLANE ONLY: no protected attribute / proxy column (data_planes.PROTECTED_KEYS +
-- NETWORK_FEATURE_KEYS; enforced by tests/test_platform_db.py). Nothing here joins match_results.
-- migrate() is version-gated (schema_version) and THREAD-locked per process only; a second
-- process or a crash mid-script can re-run this file, so it is written re-run tolerant:
-- scratch tables dropped up front, all CREATEs are IF NOT EXISTS, and no bare ALTER ADD COLUMN
-- lives here (those go through _COLUMN_UPGRADES in db.py, which is idempotent).

DROP TABLE IF EXISTS graph_edges_v2;      -- re-run tolerance (crash between DROP and RENAME)
DROP TABLE IF EXISTS applications_v2;

-- === (1) graph_edges rebuild — extend kind + provenance CHECKs (SQLite can't ALTER a CHECK).
-- Copy-rename dance exactly like 003's consents rebuild; all 17 columns preserved in 003's
-- exact order; all three indexes recreated below. NEW kinds: peer_coattendance (C3),
-- classmate/org_comember (C6), mentorship (C4). NEW provenance: affiliation (C6).
CREATE TABLE graph_edges_v2(
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
INSERT INTO graph_edges_v2 SELECT * FROM graph_edges;
DROP TABLE graph_edges;
ALTER TABLE graph_edges_v2 RENAME TO graph_edges;
CREATE UNIQUE INDEX IF NOT EXISTS idx_gedges_key ON graph_edges(edge_key);
CREATE INDEX IF NOT EXISTS idx_gedges_a ON graph_edges(user_a, school_id, consent_state);
CREATE INDEX IF NOT EXISTS idx_gedges_b ON graph_edges(user_b, school_id, consent_state);

-- === (1b) A14 one-off cleanup: revoke interview edges minted from non-accepted slots
-- (declined/cancelled interviews inflated pathfinder rank; revoked = durable, builder
-- never un-revokes). Runs before the builder gains its status='accepted' filter. Idempotent.
UPDATE graph_edges SET consent_state='revoked', revoked_at=strftime('%s','now')
WHERE kind='interview' AND consent_state != 'revoked' AND NOT EXISTS(
    SELECT 1 FROM interview_slots i JOIN applications a ON a.id=i.application_id
    WHERE i.status='accepted'
      AND ((a.student_id=graph_edges.user_a AND i.proposed_by=graph_edges.user_b)
        OR (a.student_id=graph_edges.user_b AND i.proposed_by=graph_edges.user_a)));

-- === (2) applications rebuild — add 'withdrawn' (B7). Copy-rename; inline UNIQUE + the one
-- explicit index recreated.
CREATE TABLE applications_v2(
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
INSERT INTO applications_v2 SELECT * FROM applications;
DROP TABLE applications;
ALTER TABLE applications_v2 RENAME TO applications;
CREATE INDEX IF NOT EXISTS idx_applications_posting ON applications(posting_id);

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
```

**Plus, in `stores/db.py` `_COLUMN_UPGRADES` (D4, D5, feasibility M1):**
```python
_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "users": {
        ... existing ...,
        "alumni_status": "TEXT NOT NULL DEFAULT 'none' "
                         "CHECK(alumni_status IN ('none','self_claimed','verified'))",
    },
    "intro_requests": {
        "origin": "TEXT NOT NULL DEFAULT 'organic' CHECK(origin IN ('organic','bridged'))",
    },
    "campus_events": {
        "checkin_code": "TEXT",
    },
}
```
(`_ensure_columns` is idempotent and runs on every `migrate()` *after* migration files apply, so it covers fresh DBs, partially-migrated DBs, and legacy bare users tables identically. 004 itself touches neither of these tables.)

**Plus, in `stores/data_planes.py` (privacy F2):**
```python
PROTECTED_KEYS = { ... existing ..., "grad_year", "graduation_year" }   # age proxies — no column,
                                                                        # no feature dict, ever
NO_SCORING_ATTRIBUTE_KEYS = {"alumni_status"}   # legal as a users column; barred from any
                                                # scoring feature dict by assert_no_protected
```
`InMemoryDataPlanes.assert_no_protected` gains a third check over `NO_SCORING_ATTRIBUTE_KEYS` with its own error message. (A `NO_SCORING_ATTRIBUTE_KEYS` entry is *not* checked by the platform-column CI grep — `alumni_status` is a legitimate column; it must simply never enter a feature dict.)

**Audit-plane tables (created by `AuditDB.__init__`, NOT in 004 — D11):**
```sql
CREATE TABLE IF NOT EXISTS report_snapshots(
    report_key TEXT NOT NULL,          -- 'self_id' | 'intro_equity' | 'intro_outcomes'
    school_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    refs_count INTEGER NOT NULL,
    computed_at REAL NOT NULL,
    PRIMARY KEY(report_key, school_id)
);
```

**CI guard note:** every new column name above was checked against `data_planes.PROTECTED_KEYS` and `NETWORK_FEATURE_KEYS` — no collision (`test_platform_db.py` must stay green with **no allowlist edit**). `grad_year` joins `PROTECTED_KEYS` precisely because no such column exists anywhere: any future attempt to add one trips CI.

---

## 2. Store-layer changes (exact signatures)

### 2.1 `resume_matcher/stores/relationships.py`
```python
EDGE_STRENGTH = {  # additions only — existing values unchanged
    ..., "mentorship": 0.75, "classmate": 0.50, "org_comember": 0.45,
    "peer_coattendance": 0.40,
}
```
- **D16 (feasibility H1)** — `upsert_edge` gains `seen_at: float | None = None`:
  ```python
  def upsert_edge(self, conn, school_id: int, user_a: int, user_b: int, kind: str, *,
                  provenance: str, weight: float | None = None, provenance_ref: str = "",
                  consent_state: str = "pending", owner_user_id: int | None = None,
                  expires_at: float | None = None, seen_at: float | None = None) -> None
  ```
  `seen = seen_at if seen_at is not None else now`. INSERT uses `seen` for `last_seen_at`; UPDATE becomes `last_seen_at=MAX(last_seen_at, :seen)` and bumps `observation_count` **only when `last_seen_at` actually advances** (re-folding history no longer inflates counts). Every fold in `build_native_edges` passes the source interaction timestamp (`a.created_at`, `i.created_at`, `m.sent_at`, `event_checkins.at`, affiliation `updated_at`); interactive writers (offer-accept, vouch) pass nothing and get `now`.
- **A14** — in `build_native_edges`, interview fold adds `AND i.status='accepted'` (`relationships.py:113-117`; migration section 2b already revoked historical bad edges — it runs
  after BOTH table rebuilds because it reads `applications`, which section 2 may be re-creating from
  its scratch on a recovery re-run). All application/posting folds and `_hiring_manager`-adjacent queries add `AND p.created_by != 0` (erasure sentinel — feasibility M5).
- **A13** — `promote_shareable(self, school_id: int) -> int`: add to the UPDATE's WHERE, for both endpoints:
  ```sql
  AND ge.last_seen_at >= (SELECT MAX(c.granted_at) FROM consents c
        WHERE c.user_id=graph_edges.user_a AND c.purpose='graph_discoverable'
        AND c.revoked_at IS NULL)
  ```
  (and the `user_b` twin). An edge whose interaction pre-dates the *current* consent grant of either endpoint stays `pending`; a genuinely new interaction advances `last_seen_at` (D16), so live relationships promote on the next interaction. **Test (`test_backfill_ignores_preconsent_interactions`) must be end-to-end**: interact → grant `graph_discoverable` (which enqueues `build_edges`, platform.py:972-975) → run the job → edge still `pending`. A unit test of the SQL predicate alone does not prove the fix.
- **C3** — new fold inside `build_native_edges` (same transaction), guarded by `RM_PEER_EDGE_MAX_CHECKINS` (default 150):
  ```python
  def _fold_peer_coattendance(self, conn, school_id: int) -> int
  ```
  Set-based per D9: an event is processed only if it is published, its student check-in count ≤ cap, and it has at least one `event_checkins.at` newer than the newest `peer_coattendance` edge carrying `provenance_ref=event_id` (watermark skip). For each processed event, ONE `INSERT ... SELECT` over the self-join of its student check-ins (`u1.user_id < u2.user_id`, both `role='student'`) with `ON CONFLICT(edge_key) DO UPDATE SET observation_count=observation_count+1, last_seen_at=MAX(last_seen_at, excluded.last_seen_at), updated_at=excluded.updated_at WHERE graph_edges.consent_state != 'revoked'`, excluding endpoints present in `graph_suppressions` (both `upsert_edge` invariants — suppression skip, never-resurrect-revoked — must hold in the set-based form; tested). Per-pair `seen_at` = the later of the two check-in times. **Expected cost:** ≤ C(150,2) = 11 175 rows in one statement, only for events with new check-ins; steady-state rebuilds touch zero events.
  Also *upgrade* the existing student↔employer `event_coattendance` fold to prefer `event_checkins` when any exist for the event (verified presence beats RSVP), keeping the RSVP fold as fallback.
- **C6** — new fold, called from `build_native_edges`:
  ```python
  def _fold_affiliation_edges(self, conn, school_id: int) -> int
  ```
  **Attestation pairs only (D15):** one edge per claim `c` with `c.status='confirmed' AND c.confirmed_by IS NOT NULL`, between `c.user_id` and `c.confirmed_by`, provided the confirmer's own claim on the same affiliation is `status='confirmed'`. `kind='classmate'` (course_section) / `'org_comember'` (club), `provenance='affiliation'`, `provenance_ref=affiliation_id`, `expires_at=now+365d` (self-asserted data is retention-bounded like self_upload), `seen_at=c.updated_at`. **Never a clique fold**: two colluders who mutually confirm gain exactly one edge — between themselves (security H2 test).
- **C4** — mentorship edge writes happen from the offer-accept path (phase5 router) via existing `upsert_edge(conn, school, student, mentor, "mentorship", provenance="alumni", provenance_ref=offer_id, consent_state="pending")`.
- **B10 fixes** —
  ```python
  def vouches_for_coordinator(self, school_id: int, status: str = "contested") -> list[dict]
      # status in ('contested','self'); joins voucher+subject emails; includes contested_note
  def resolve_vouch(self, school_id: int, vouch_id: str, coordinator_id: int, action: str) -> dict
      # school-scoped (D13): WHERE id=? AND school_id=?; raises NotFound-equivalent otherwise.
      # action='verify'  -> verify_vouch(..., 'coordinator') + status back to 'active'
      # action='dismiss' -> status='withdrawn'; provenance_ref edges stay revoked
  ```
- **C7 hook** — in `create_vouch`, new keyword `via_invite_id: str | None = None` recorded for queue display only (no schema change; the invite row links `vouch_id`). No tier suggestion of any kind derives from affiliations (D14).

### 2.2 `resume_matcher/stores/graph.py`
- **A17** — `delete_my_network`: add
  `conn.execute("DELETE FROM broker_blocks WHERE broker_user_id=? OR blocked_user_id=?", (user_id, user_id))` (docstring now true).
- **A1 (store half)** — replace public instant `repudiate` with two scoped executors + the queue. **The old `repudiate()` method is deleted** (no caller may bypass the challenge/queue), and a test asserts no route reaches an executor directly (security L2).
  ```python
  def repudiate_execute_email(self, school_id: int, *, email: str) -> dict
      # EMAIL PATH ONLY (caller: confirm_repudiation — the requester PROVED control of the
      # address, so member-scoped deletion is legitimate self-action): tokenize email in RAM;
      # INSERT graph_suppressions(identity_token,'third_party_repudiation'); for each member
      # uid whose member_graph_identity matches the token, delete ONLY edges with
      # provenance='self_upload' touching uid — NEVER native edges; delete the matching
      # member_graph_identity rows only (not the member's other tokens).

  def repudiate_execute_name(self, school_id: int, *, first: str, last: str,
                             company: str) -> dict
      # NAME PATH (caller: decide_repudiation on coordinator approval). NEVER touches member
      # data (privacy F3): if the name token matches member_graph_identity of any ACTIVE
      # member, that member's rows/edges are left untouched and NO member-scoped suppression
      # row is inserted — instead a 'repudiation_notice' notification points the member at
      # their own self-serve controls (delete-my-network / account erasure). For non-member
      # identities: insert the suppression token (blocks future self-upload derivation) and
      # hard-delete employer_contacts rows (+ their posting_contacts refs) whose
      # display_label matches the normalized name token (privacy F5 match path).
      # Returns {"member_matched": bool, "contacts_deleted": int} for the decision receipt.

  def create_repudiation(self, school_id: int, *, kind: str, email: str = "", first: str = "",
                         last: str = "", company: str = "") -> dict
      # first/last/company: length-cap 80 chars + redact_text() AT INGEST (security H1).
      # kind='email_challenge': store sha256(email), generate token, store sha256(token),
      #   expires_at=now+48h; RETURNS {"request_id", "email_token"} (token only for the mailer,
      #   never persisted in cleartext, never returned to the HTTP caller).
      #   ANTI-BOMBING (security H3): refuse (silently — same 202 shape) when this email_hash
      #   already received RM_REPUDIATE_MAX_PER_EMAIL (default 3) challenges in 24h, or the
      #   global 24h send count exceeds RM_REPUDIATE_MAX_EMAILS_PER_DAY (default 50). Both
      #   caps are independent of source IP.
      # kind='name_review': store redacted fields for coordinator review; status='pending';
      #   expires_at=now + RM_REPUDIATE_REVIEW_TTL_DAYS (default 30) days (privacy F6).

  def confirm_repudiation(self, request_id: str, email: str, token: str) -> dict
      # sha256 both, compare constant-time, check expiry -> repudiate_execute_email(email=email);
      # status='confirmed', scrub email_hash/challenge_hash, purge_after=now+30d.

  def decide_repudiation(self, school_id: int, request_id: str, coordinator_id: int,
                         approve: bool) -> dict
      # school-scoped (D13): WHERE id=? AND school_id=?; 404-equivalent otherwise.
      # approve -> repudiate_execute_name(first,last,company); either way scrub
      # first/last/company, status='approved'/'denied', purge_after=now+30d.

  def list_repudiations(self, school_id: int, status: str = "pending") -> list[dict]
      # each name_review row includes a non-destructive match PREVIEW computed at read:
      # {"member_matched": bool, "contact_matches": int} so the coordinator sees what an
      # approval will (and will not) do before clicking (security L2). No member identity
      # is disclosed — just the boolean.
  ```

### 2.3 `resume_matcher/stores/intros.py`
- **A2** — `find_paths(rel, requester_id, target_id, school_id, *, max_depth=MAX_DEPTH, top_k=TOP_K, now=None, broker_ok: Callable[[int], bool] | None = None)`: when a completed path is appended, skip it unless `broker_ok is None or broker_ok(new_nodes[1])`. Routes pass `lambda uid: StudentStore().has_consent(uid, "warm_intro")` (memoized per call). No-consent brokers are pruned *before* ranking, so the response shape is identical to "no path" (no consent oracle).
- **A2 TOCTOU (security M2)** — `IntroStore.create` performs the FINAL broker `warm_intro` check **inside its own transaction**: `BEGIN IMMEDIATE`, `SELECT` the live consent row on the same connection, abort (`IntroError`) if gone, then `INSERT`. The route-level re-check is advisory; the in-transaction check is the invariant.
- **C2** — `IntroStore.create(..., origin: str = "organic")`: new column in the INSERT. The route computes `origin = "bridged" if any(k in ("alumni_bridge","mentorship") for k,_ in path["edges"]) else "organic"`.
- **C2 report** —
  ```python
  def outcome_rows(self, school_id: int) -> list[dict]
      # per intro_request: origin, broker edge kind (from path_json edges[0]), status,
      # + LEFT JOIN applications on application_id for terminal status.
  ```

### 2.4 `resume_matcher/stores/students.py`
- **B7** — `_APP_TRANSITIONS`: add `"withdrawn"` to the reachable set of `applied`, `shortlisted`, `advanced` (terminal; nothing leaves `withdrawn`).
  ```python
  def withdraw(self, app_id: str, student_id: int) -> dict
      # ownership-checked (WHERE id=? AND student_id=?); StudentError if terminal already.
  ```
  `set_status` gains a guard: `if to_status == "withdrawn": raise StudentError("Students withdraw their own applications.")` (employers can't withdraw for them). `for_posting()` adds `include_withdrawn: bool = False` (default excludes). `_funnel_rows` (platform.py) excludes withdrawn from `applied` denominators via `status != 'withdrawn'`.
- **A7 helper** —
  ```python
  def filter_by_consent(self, user_ids: list[int], purpose: str) -> list[int]
      # single IN(...) query against consents; preserves order.
  ```
- **C4** — alumni helpers (users table column, D4; school-scoped per D13 / security C2):
  ```python
  def set_alumni_status(self, user_id: int, school_id: int, status: str,
                        attested_by: int | None = None) -> None
      # UPDATE users SET alumni_status=? WHERE id=? AND school_id=?  -> 0 rows = NotFound
  def alumni_queue(self, school_id: int) -> list[dict]        # status='self_claimed'; NO grad_year
  ```

### 2.5 `resume_matcher/stores/engage.py`
- **C3** —
  ```python
  class EventStore:
      def set_checkin_code(self, event_id: str, school_id: int) -> str   # token_urlsafe(6), stored
      def checkin_by_code(self, event_id: str, user_id: int, code: str) -> None
          # published event, code matches (constant-time), user holds a 'registered' registration
      def checkin_roster(self, event_id: str, user_id: int, coordinator_id: int,
                         school_id: int) -> None
          # D13: event AND target user must both belong to school_id (security M3 rider)
      def checkins(self, event_id: str, school_id: int) -> list[dict]
  ```
  The code route is rate-limited at the API layer (§3.2): the code is a low-entropy shared secret and is only a *second* factor on top of the required registration.

### 2.6 `resume_matcher/stores/audit_store.py`
- **A8** — `aggregate(candidate_refs, attr)` hardening:
  - **cohort floor:** if `responses < 2 * MIN_CELL` → return `{"counts": {}, "suppressed_cells": len(counts), "responses": None, "note": "cohort below reporting floor"}` (no exact total).
  - **complementary suppression:** whenever `suppressed_cells > 0`, `responses` is banded to the nearest `MIN_CELL` multiple and returned as a string band (e.g. `"35-40"`), never the exact integer — the subtract-the-visible-cells channel is closed.
  - The two pinned tests this breaks (`test_min_cell_suppression` responses==9; `test_self_id_report_uses_aligned_refs` responses==5) are updated **in the same slice** (S3 — feasibility H2).
- **A8 snapshots** —
  ```python
  def save_snapshot(self, report_key: str, school_id: int, payload: dict, refs_count: int) -> None
  def get_snapshot(self, report_key: str, school_id: int) -> dict | None   # {payload, refs_count, computed_at}
  ```
  Serving policy lives in the route (§3): recompute only if snapshot older than `RM_AUDIT_SNAPSHOT_HOURS` (default 24) **and** the ref-set size changed by ≥ MIN_CELL since the snapshot; otherwise serve the pinned snapshot. This kills before/after single-student differencing.
- **A6/A3** — `delete_self_id` already exists; no change needed (callers added).

### 2.7 `resume_matcher/audit/metrics.py`
- **A4 `access_disparity`** — for a group with `denom >= min_cell` and absent numerator: bound the true rate in `[0, (min_cell-1)/denom]`, store `{"rate": None, "rate_bound_upper": ub, "note": "below reporting threshold", "denom": denom}`. Pass logic:
  - `four_fifths_pass=True` requires **every** eligible group (computable *or* bounded) to have `rate-or-upper-bound >= 0.8 * max_rate` — unreachable while any bounded group's upper bound is below 0.8·max.
  - if any *bounded* group's upper bound < 0.8·max → `four_fifths_pass=False` + note `"suppressed group cannot exceed 0.8 threshold — human review required"`.
  - if bounds straddle (can't prove either way) → `four_fifths_pass=None` + `"indeterminate under suppression — human review required"`.
  - `< 2` groups with a usable value → `None` (existing behavior retained).
  - remove the dead `max(...) or 1.0` pattern.
  - `tests/test_phase4_fairness.py` pins the OLD `None` outcome for the bounded-fail case (line 31) — that expectation flips to `False` and is updated in the same slice (S3 — feasibility H2).
- **A4 `selection_audit`** — require ≥ 2 eligible (above-min-cell) groups: else `four_fifths_pass=None`, note `"fewer than 2 comparable groups"`. If `total selections == 0` → `four_fifths_pass=None`, note `"no selections yet — audit not meaningful"`; remove `or 1.0`.
- **C2** — new pure function:
  ```python
  def origin_impact(counts: dict[str, dict[str, int]], min_cell: int = 5) -> dict
      # counts = {"organic": {"requested": n, "accepted": n, "shortlisted": n, "hired": n},
      #           "bridged": {...}}; per-stage conversion + bridged/organic ratio;
      # any stage count < min_cell -> that ratio is None with "below threshold".
  ```

### 2.8 `resume_matcher/audit/compliance_pack.py`
- **A4 follow-through:** wherever a pack embeds `four_fifths_pass`, `None` renders as `"indeterminate"` (never coerced to a boolean); pack version string bumped (`pack_semantics: "v2-bounded-suppression"`) so previously signed packs are distinguishable.

### 2.9 `resume_matcher/audit/proxy_leakage.py` (A12)
- `_auc`: midrank ties (0.5 credit per tied pair).
- Fallback regression: standardize features first, then append the ones column (intercept survives standardization).
- `cv = max(2, min(5, positives, negatives))`; emit `"method": "logreg_cv" | "fallback_linear"` explicitly instead of the blanket `except Exception` swap.

### 2.10 `resume_matcher/inference/adapters/mock.py` (A11)
- `mock._find_span` iterates `taxonomy.surface_forms(skill_id)` (already public and alias-inclusive, `taxonomy.py:166-172` — feasibility confirmed no re-export is needed; `taxonomy.py` is untouched) instead of the private canonical-only `_surface_forms`, keeping the ≥ 2-alnum-char guard.

### 2.11 NEW `resume_matcher/stores/notifications.py` (B4-lite)
```python
class NotificationStore(_Store-pattern):
    def notify(self, user_id: int, school_id: int, kind: str, title: str,
               body: str = "", entity: str | None = None, entity_id: str | None = None,
               *, email_to: str | None = None) -> int
        # inserts row; if email_to given ALSO fires notify.send(email_to, title, body)
        # (best-effort, existing no-op contract when RM_SMTP_HOST unset).
        # INVARIANT: composed title/body never embed a user email address (test-enforced —
        # otherwise erasure leaves the erased user's email in other users' rows).
    def feed(self, user_id: int, *, unread_only: bool = False,
             page: int = 1, page_size: int = 20) -> dict      # {items, unread, page}
        # SELECT carries WHERE user_id=? (security L1)
    def mark_read(self, user_id: int, ids: list[int] | None = None) -> int   # None = all
        # UPDATE carries WHERE user_id=? AND id IN (...) — a supplied ids list can never
        # touch another user's rows (security L1)
    def purge(self, *, read_older_than_s: float = 90*86400,
              unread_older_than_s: float = 180*86400) -> int   # called by run_retention
```
Fan-out call sites (all wrapped best-effort like `_notify_creator`): message send → other thread participants; interview propose/cancel → student; intro create → broker (`intro_request`); intro **accept** → requester (`intro_accepted`); intro **decline → NO notification** (D8); vouch create → subject; vouch contest → school coordinators; application status change (by employer) → student; posting approve/reject → creator (replaces the email-only `_notify_creator` — now row + email); mentorship offer → mentor; mentorship **accept** → student; mentorship **decline → NO notification to anyone** (D8); affiliation claim **confirmed** → the claim owner (the draft's "confirm request → listed peer" broadcast is **removed** — privacy F1 aggravator; confirmation is solicited via confirm-links, §2.13); coordinator bridge → mentor + student; name-review repudiation matching an active member → that member (`repudiation_notice`, privacy F3).

### 2.12 NEW `resume_matcher/stores/erasure.py` (A3) — see §6 for the cascade contract
```python
class ErasureError(Exception): ...
def erase_account(user_id: int, *, reason: str = "member_deleted",
                  dry_run: bool = False) -> dict
    # returns {"tables": {name: rows_deleted_or_would_delete}, "tombstoned": bool,
    #          "audit_plane_deleted": bool}
```

### 2.13 NEW `resume_matcher/stores/phase5.py` (mentorship / affiliations / vouch invites / ERM)
```python
class Phase5Error(Exception): ...

class MentorStore:
    def upsert_profile(self, user_id: int, school_id: int, *, program: str, topics: str,
                       capacity: int, active: bool) -> dict
    def delete_profile(self, user_id: int) -> bool                    # the opt-out (D1)
    def get_profile(self, user_id: int) -> dict | None
    def eligible_mentors(self, school_id: int) -> list[dict]
        # active profile AND warm_intro consent AND graph_discoverable consent AND
        # (users.alumni_status='verified' OR role IN ('employer','coordinator'))
        # AND accepted-offer count < capacity — all rows school_id-scoped
    def create_offer(self, *, school_id: int, student_user_id: int, mentor_user_id: int,
                     origin: str, rationale: str) -> dict
        # 30-day expires_at. D13: asserts BOTH users belong to school_id (404-equivalent
        # otherwise — security C1). Pair cooldown: refuses (neutral Phase5Error, same
        # message as "open offer exists") if any offer for this pair is younger than
        # RM_MENTOR_OFFER_COOLDOWN_DAYS (default 90) — decline stays unobservable (D8).
    def offers_for_mentor(self, mentor_user_id: int) -> list[dict]     # status='offered'
    def respond_offer(self, offer_id: str, mentor_user_id: int, accept: bool) -> dict
        # mentor-only identity check (accept/decline mirrors intro broker discipline);
        # accept -> RelationshipStore.upsert_edge(kind='mentorship') + promote_shareable
    def mentorship_stats(self, school_id: int) -> dict
        # D8/privacy F9: the ONLY coordinator-visible mentorship telemetry — aggregate
        # counts {offers_made, accepted, active_mentors}, each suppressed below MIN_CELL.
        # No per-offer, per-student, or per-mentor status is ever exposed to coordinators.
    def sweep_expired(self) -> int

class AffiliationStore:
    def claim(self, *, user_id: int, school_id: int, kind: str, label: str,
              term: str = "", claim_role: str = "member") -> dict
        # normalizes label -> label_norm; get-or-create affiliation; per-user claim cap
        # (RM_AFFILIATION_MAX_CLAIMS, default 30) — anti-spam/enumeration.
        # claim_role is stored for display only (D14).
    def mine(self, user_id: int) -> list[dict]
        # own claims + affiliation labels + confirm state + each claim's confirm_url
        # ('/student#affil-confirm={claim_id}') — the claimant shares THEIR OWN link
        # out-of-band with a peer who can attest them (D15). claim ids are
        # token_urlsafe(16) capabilities, not enumerable.
    def claimants(self, affiliation_id: str, viewer_user_id: int) -> list[dict]
        # PRIVACY F1 (hard requirement): 404 unless the viewer holds a CONFIRMED claim on
        # this affiliation; returns ONLY co-claimants whose own status='confirmed', as
        # {claim_id, email_masked, claim_role, status} — email masked to first char +
        # domain ('a***@yorku.ca'). Full email is disclosed only between an attestation
        # pair (viewer confirmed them or they confirmed viewer). An unconfirmed claim
        # grants ZERO read visibility (test: unconfirmed claimant on a 50-member
        # affiliation gets 404).
    def confirm(self, claim_id: str, confirmer_user_id: int) -> dict
        # Reached via confirm-link only. Rules: confirmer holds their own claim on the
        # same affiliation; confirmer != claimant; per-user daily cap
        # RM_AFFILIATION_MAX_CONFIRMS_PER_DAY (default 20, security H2).
        # - confirmer's claim already 'confirmed' -> target claim flips to 'confirmed',
        #   confirmed_by=confirmer.
        # - both unconfirmed (bootstrap): first direction records confirmed_by on the
        #   target claim but leaves status='unconfirmed'; when the reciprocal confirm
        #   lands, BOTH flip to 'confirmed' atomically (mutual attestation pair).
        # Enqueues build_edges job on any flip to 'confirmed'.
    def remove_claim(self, claim_id: str, user_id: int) -> bool
        # own claim only; hard delete; HARD-DELETES (not revokes — feasibility L3, so a
        # later genuine re-claim + re-confirm can re-mint) derived affiliation edges:
        # kind IN ('classmate','org_comember') AND provenance_ref=affiliation_id AND the
        # remover is an endpoint.
```
(The draft's `has_confirmed_role` is **removed** — D14.)
```python
class VouchInviteStore:
    def create(self, *, subject_user_id: int, school_id: int,
               relationship_hint: str | None) -> dict
        # {invite_token (cleartext, returned ONCE), expires_at}; stores sha256(token)
        # only (D10/feasibility L2). Cap: 10 open invites per subject.
    def get_open(self, token: str, school_id: int) -> dict | None
        # hash lookup; None if used/revoked/expired OR wrong school (cross-school = 404,
        # security M1/feasibility L2)
    def consume(self, token: str, voucher_user_id: int, voucher_school_id: int,
                vouch_id: str) -> None
        # REJECTS (Phase5Error -> neutral 404) when voucher_school_id != invite school:
        # the vouch and any derived edge are always written in the INVITE's school and
        # only by a same-school voucher (security M1 — decided: reject, not remap).
    def revoke(self, token_or_id: str, subject_user_id: int) -> bool
    def sweep_expired(self) -> int

class ErmStore:  # C5 coordinator org-engagement view (read-only rollup)
    def org_engagement(self, school_id: int) -> list[dict]
        # per approved/pending org: link_status, postings (live/closed counts),
        # applications, hires, events attended (event_registrations role='employer'),
        # last activity timestamp. Pure counts — no student identities.

class ContactStore:  # C5 employer-owned contacts (PIPEDA business-contact exemption)
    def add_contact(self, *, org_id: int, school_id: int, added_by: int,
                    display_label: str, role_title: str,
                    contact_user_id: int | None) -> dict
        # display_label AND role_title: length-capped, redact_text() (the vouch-evidence
        # ingest chokepoint, relationships.py:195 pattern) THEN escaped + CSV-neutralized
        # (leading = + - @ tab CR) at write — emails/phones cannot survive into a field
        # the erasure/repudiation machinery can't reach (privacy F5). Free-text contacts
        # (contact_user_id IS NULL) may hold business role/title text ONLY; documented in
        # PRIVACY.md. contact_user_id, when given, MUST be a member of org_id (security
        # M4) — arbitrary/cross-org/cross-school ids rejected.
    def list_contacts(self, org_id: int) -> list[dict]
    def delete_contact(self, contact_id: str, org_id: int) -> bool
        # THE C5 deletion path: hard-deletes the employer_contacts row AND all
        # posting_contacts rows referencing it (cascade); returns False if not owned
    def set_posting_contact(self, *, posting_id: str, school_id: int, added_by: int,
                            contact_user_id: int | None = None,
                            employer_contact_id: str | None = None,
                            relation: str = "hiring_manager") -> dict
        # replaces any existing posting_contacts row for the posting (one target);
        # contact_user_id subject to the same own-org membership check (security M4)
    def clear_posting_contact(self, posting_id: str) -> bool
    def contacts_for_user(self, user_id: int) -> int
        # erasure hook: DELETE employer_contacts WHERE contact_user_id=? and their
        # posting_contacts references (called by erase_account)
```

### 2.14 `resume_matcher/stores/platform.py` (B6)
```python
class PostingStore:
    def search(self, *, school_id: int, status: str = "live", q: str = "",
               employment_type: str = "", work_mode: str = "", pay_min: float | None = None,
               deadline_after: str = "", sort: str = "newest",
               page: int = 1, page_size: int = 20) -> dict
        # {postings, total, page, page_size}; q -> parameterized LIKE over
        # title/description/org name with an explicit ESCAPE '\' clause, escaping
        # BOTH '%' AND '_' (and '\') in the user term (security L4);
        # sort in ('newest','deadline','pay'); page_size <= 50
```

### 2.15 `resume_matcher/stores/retention.py`
`run_retention()` additionally calls: `NotificationStore().purge()`, `MentorStore().sweep_expired()`, `VouchInviteStore().sweep_expired()`, purges `repudiation_requests` past `purge_after`, expires + **scrubs `first/last/company` on** `name_review` rows past `expires_at` that no coordinator ever decided (privacy F6 — third-party PII never persists indefinitely), expires unconfirmed email challenges, and deletes expired `admin_sessions` rows (security M5 — lazy purge alone leaves never-revisited rows).

### 2.16 `resume_matcher/api/auth.py` + `api/accounts.py` (A15/A16)
```python
# auth.py
def _pw_fingerprint() -> str            # sha256("rm-admin:" + RM_ADMIN_PASSWORD).hexdigest()[:16]
def create_admin_session() -> str       # random token; sha256 + current pw_fingerprint stored,
                                        # expiry now + RM_ADMIN_SESSION_HOURS (default 12)
def validate_admin_session(cookie: str) -> bool
    # hash lookup + expiry + pw_fingerprint == current (password rotation therefore
    # invalidates ALL outstanding sessions — security M5); lazy purge of expired rows
def destroy_admin_session(cookie: str) -> None
def check_login(username, password) -> str | None  # now returns create_admin_session()
def assert_admin_password_strong() -> None
    # RM_ENV=prod: RAISES RuntimeError when password unset, weak, or RM_COOKIE_SECURE
    # is explicitly off. Non-prod: warning only (demo posture unchanged).
```
`require_auth` swaps the constant-time HMAC compare for `validate_admin_session`. The cookie keeps `HttpOnly` + `SameSite=Lax`, with `Secure` tied to `RM_COOKIE_SECURE` (asserted on in prod). `app.py` `/api/logout` calls `destroy_admin_session`. `scripts/deploy.sh` **and `scripts/deploy.ps1`** + `deploy/cohost/.env.example` gain the prod note (feasibility L6). `accounts.py`: `user_for_token` SELECT adds `u.alumni_status` to the returned user dict (needed by C4 routes and the `_BROKER_VERIFY_LEVEL` mapping).

`_PLATFORM_PREFIXES` additions are split by slice ownership (feasibility M2/M3): the Phase-5 router prefixes **and** the exact-path entry `"/api/account"` (the existing tuple only has `"/api/account/"` with a trailing slash — without the bare entry, `DELETE /api/account` would 401 behind the admin gate before `require_role` runs) land together with the router mount in S6.

---

## 3. API changes

### 3.1 In-place edits in `resume_matcher/api/platform.py` (A-items + B6/B7)

| Route | Change |
|---|---|
| `POST /api/graph/repudiate` (public) | **A1.** New limiter instance `_repudiate_rate = _RateLimiter(3, 5/3600)` keyed on `_client_key(request)` and called as `.allow(key, time.time())` (two-arg signature — feasibility L5). `_client_key` is **duplicated as a 4-line local helper** rather than imported from `.app` (avoids dragging the demo module graph into platform import — feasibility L5). NOTE: `_client_key` trusts the first `X-Forwarded-For` hop and is only meaningful behind the trusted Caddy front; the per-email and global caps in `create_repudiation` (§2.2) are the real anti-bombing backstop and are IP-independent (security H3). Body with `email` → `create_repudiation(kind='email_challenge')`, send challenge email via `notify.send`, return `202 {"status":"challenge_sent","request_id"}` (same shape whether or not the email matches anything, and same shape when a send-cap silently swallowed the email — no oracle). Body without email → `create_repudiation(kind='name_review')`, return `202 {"status":"queued_for_review","request_id"}`. **No instant deletion on this route anymore.** |
| `POST /api/graph/repudiate/confirm` (public, new, same limiter) | Body `{request_id, email, token}` → `confirm_repudiation` → executes the email-scoped deletion. `200 {"ok": true}` / `400`. |
| `GET /api/coordinator/repudiations` + `POST /api/coordinator/repudiations/{id}/decide` | `require_role("coordinator","admin")`; **school-scoped** (D13): both calls pass `user["school_id"]`; a cross-tenant `id` yields 404. Queue rows include the match preview (§2.2). Body `{approve: bool}` → `decide_repudiation`. |
| `GET /api/intros/available/{posting_id}`, `POST /api/intros/requests` | **A2.** Pass `broker_ok=lambda uid: store.has_consent(uid, "warm_intro")` into `find_paths`; the binding consent check lives INSIDE `IntroStore.create`'s transaction (security M2). Same neutral 404/409 shapes. **C2:** compute `origin` from the chosen path and pass to `create`. |
| `POST /api/students/me/consents` (`set_consent` route) | **A5:** on `resume_storage` revoke → `StudentStore().delete_resume(uid)` + `MatchStore().delete_for_student(uid)` (mirrors platform.py:450-451). **A6:** on `self_id_audit` revoke → `AuditDB().delete_self_id(f"student-{uid}")`. Same branches added to `set_graph_consent` if the purposes ever route there (they don't today — assert with a test). |
| `GET /api/coordinator/reports/intro-equity` | **A7:** wrap each of applicants/requested/converted with `StudentStore().filter_by_consent(ids, "network_analytics")` before `_refs`. **A8:** serve via snapshot policy (`AuditDB.get_snapshot("intro_equity", school)` / `save_snapshot`), recompute gated by age + ref-count delta ≥ MIN_CELL. |
| `GET /api/coordinator/reports/self-id` | **A8 only:** snapshot policy (`report_key='self_id'`); banded `responses` comes free from the `aggregate()` change. **The self-id cohort keeps `self_id_audit` as its consent basis — the `network_analytics` filter is NOT applied here** (feasibility H3: applying it empties the report for every existing consenting student; A7 names only intro-equity and network-coverage). |
| `GET /api/coordinator/network-coverage` | **A7 (previously missing — feasibility H3):** the aggregate counts (platform.py:1246-1249) are computed over the cohort filtered by `network_analytics` consent. |
| `GET /api/postings` | **B6:** student branch now accepts `q, employment_type, work_mode, pay_min, deadline_after, sort, page, page_size` query params → `PostingStore.search(...)`; employer/coordinator branches unchanged (existing `list`). Response for students becomes `{postings, total, page, page_size}` (UI updated in the same phase; other roles keep `{postings}`). |
| `POST /api/applications/{application_id}/withdraw` | **B7:** `require_role("student")` → `ApplicationStore().withdraw(id, user["id"])` → `{"status":"withdrawn"}`; a plain `application_status`-kind notification to the posting creator with server-composed body ("An applicant withdrew") — no reason text is ever collected. |
| `PATCH /api/applications/{application_id}` | **B7 guard:** reject `status='withdrawn'` with 409. **A9 support:** unchanged otherwise (transitions already complete). On success → notification to the student (`application_status`). |
| `_hiring_manager(posting)` | **C5:** extended — `posting_contacts.contact_user_id`, else `employer_contacts.contact_user_id` via `posting_contacts.employer_contact_id` join, else `posting.created_by` (current behavior preserved as fallback). **Treats `created_by=0` (erasure sentinel) as None** so the pathfinder never targets uid 0 (feasibility M5; route already 409s on `target is None`). |
| `GET /api/intros/for-application/{application_id}` | **B17-adjacent spec req (RELATIONSHIPS.md:395):** add `_record_exposure(user, posting_id)` call — evidence-card views are exposure-logged. |
| `POST /api/vouches` | **B10 dead-end fixes:** pass `org_id=body.get("org_id")` through to `create_vouch`; if caller's `alumni_status == 'verified'` the later *intro-accept* mapping applies (below). |
| `accept_intro` | **C4/B10:** `_BROKER_VERIFY_LEVEL` lookup becomes a function: employer→`employer_verified`, coordinator/admin→`coordinator`, student with `alumni_status='verified'`→`alumni_verified`, else `self`. |
| funnel/EEO queries (`_funnel_rows`) | **B7:** `applied` counts exclude `status='withdrawn'`; new `withdrawn` column in the report + CSV. |
| notification fan-out | calls to `NotificationStore().notify(...)` added at the sites listed in §2.11. |

### 3.2 NEW router `resume_matcher/api/phase5.py` (mounted in `create_app` in slice S6, together with its `auth._PLATFORM_PREFIXES` additions: `/api/notifications`, `/api/mentorship`, `/api/affiliations`, `/api/alumni`, `/api/orgs/me/contacts`, `/api/account` (exact — feasibility M2), and the `/repudiate` exempt-path — feasibility M3)

| Method & path | Auth | Request → Response |
|---|---|---|
| `GET /api/notifications` | `require_role()` | `?unread=1&page=` → `{items:[{id,kind,title,body,entity,entity_id,created_at,read_at}], unread:int, page}` — own rows only |
| `POST /api/notifications/read` | `require_role()` | `{ids:[..]} or {all:true}` → `{marked:int}` — user-scoped UPDATE (security L1) |
| `PUT /api/mentorship/profile` | `require_role()` (verified alumni, employer, or coordinator — 403 otherwise) | `{program, topics, capacity, active}` → profile dict. Requires live `warm_intro` consent (409 otherwise, message says why). |
| `DELETE /api/mentorship/profile` | same | → `{deleted:bool}` (the opt-out, D1) |
| `GET /api/mentorship/offers` | `require_role()` | mentor's `status='offered'` rows: `{offers:[{id, student_email, program, rationale, created_at}]}` (student identity revealed to the mentor only at offer time — mirrors broker-inbox reveal discipline) |
| `POST /api/mentorship/offers/{id}/respond` | `require_role()` | `{accept: bool}`; mentor-only check; accept → mentorship edge + notifications; decline silent to student AND coordinator (D8) → `{status}` |
| `GET /api/coordinator/mentorship-stats` | coordinator/admin | **D8/privacy F9:** `MentorStore.mentorship_stats(school)` — the only coordinator mentorship telemetry; aggregates only, MIN_CELL'd. |
| `POST /api/alumni/claim` | `require_role("student")` | empty body → sets `alumni_status='self_claimed'` for `user["id"]` only (a student can never self-verify — security C2) → `{alumni_status}`. **No `grad_year` is collected** (privacy F2). |
| `GET /api/coordinator/alumni` | coordinator/admin | queue of `self_claimed` users **in the caller's school** → `{claims:[{user_id,email,program}]}` |
| `POST /api/coordinator/alumni/{user_id}/verify` | coordinator/admin | `{approve: bool}` → `set_alumni_status(uid, user["school_id"], ...)` → `verified` / back to `none`; cross-school user_id → 404 (security C2). Coordinator attests against SIS out-of-band; the attestation click is the record (`events` row `action='alumni_verified'`). |
| `GET /api/coordinator/under-networked` | coordinator/admin | **C1.** `{students:[{user_id,email,program,degree:0}], total}` — discoverable students with zero shareable edges **who also hold `network_analytics` consent** (decision per feasibility H3: an individual-level listing is analytics about a person's network position, so it takes the analytics consent on top of `graph_discoverable`; stated in §8). **No `grad_year`** (privacy F2/F7). **Every read appends an `events` row `action='under_networked_viewed'` (actor=coordinator)** — the roster of a disadvantaged structural class is access-logged (privacy F7). Structural trigger only; no self-ID field anywhere near this query (test-enforced). |
| `GET /api/coordinator/mentors` | coordinator/admin | **C1 picker.** `MentorStore.eligible_mentors(school)` → `{mentors:[{user_id,email,program,capacity_left,alumni_status}]}` |
| `POST /api/coordinator/mentorship-offers` | coordinator/admin | `{student_id, mentor_id}` → `create_offer(school_id=user["school_id"], origin='coordinator', rationale='coordinator bridge')` — the store 404s any cross-school id (security C1); notification to mentor; response is `202 {"status":"offered"}` with NO later status surface (D8). (The double-opt-in replacement for blind `intros/bridge` edge-minting; the existing `POST /api/coordinator/intros/bridge` remains for direct edge creation but now ALSO requires the mentor to hold `graph_discoverable`, not just `warm_intro`.) |
| `GET /api/coordinator/reports/intro-outcomes` | coordinator/admin | **C2.** `{by_origin: origin_impact(...), by_broker_kind: {...}, min_cell:5}` — intro→application-status conversion by `origin` and broker relationship kind; every cell `< MIN_CELL` suppressed; cohort filtered by `network_analytics` consent (A7 helper); served through the A8 snapshot policy (`report_key='intro_outcomes'`). CSV via `?format=csv`. |
| `POST /api/events/{event_id}/checkin-code` | coordinator/admin | **C3.** school-scoped → `{code}` (regenerates) |
| `POST /api/events/{event_id}/checkin` | student/employer | `{code}` → self check-in (must be registered). **Rate-limited per user AND per IP (`_RateLimiter(5, 5/600)`)** — the code is low-entropy and shared, so it is only a second factor on top of the registration requirement (security M3) → `{ok}` |
| `POST /api/coordinator/events/{event_id}/checkins` | coordinator/admin | `{user_id}` roster check-in — event AND target user must be in the coordinator's school (security M3/D13) → `{ok}`; both check-in routes enqueue `build_edges` (dedupe-keyed) |
| `GET /api/events/{event_id}/checkins` | coordinator/admin | school-scoped roster view `{checkins:[{user_id,email,method,at}]}` |
| `POST /api/affiliations/claim` | student (+ verified alumni) | **C6.** `{kind,label,term,claim_role}` → claim dict (unconfirmed) incl. its `confirm_url` |
| `GET /api/affiliations/mine` | `require_role()` | own claims + affiliations + confirm state + per-claim confirm-link (D15) |
| `GET /api/affiliations/{id}/claimants` | `require_role()` | **privacy F1 hard requirement:** 404 unless caller holds a **confirmed** claim; returns confirmed co-claimants only, emails masked (§2.13) |
| `POST /api/affiliations/claims/{claim_id}/confirm` | `require_role()` | confirm-link target (claim_id is the capability); store rules §2.13 → `{status}`; enqueues `build_edges` on flip |
| `DELETE /api/affiliations/claims/{claim_id}` | `require_role()` | own claim hard-delete + derived-edge hard-delete → `{deleted}` |
| `POST /api/vouches/invites` | `require_role()` | **C7.** `{relationship_hint}` → `{invite_url: "/student#vouch-invite={token}", expires_at}` (token returned once; only its hash at rest — D10) |
| `GET /api/vouches/invites/{token}` | `require_role()` | `{subject_email, relationship_hint, expires_at}` — signed-in holder of the link sees who asked; 404 if used/expired/revoked **or cross-school** (security M1) |
| `POST /api/vouches/invites/{token}/submit` | `require_role()` | `{relationship, evidence}` — **both fields pass through `redact_text()` in the handler BEFORE `create_vouch`** (this route does not share `/api/vouches`' chokepoint — security M6) → `create_vouch(verify_level='self', via_invite_id=...)` + `consume` (same-school enforced) → `{vouch_id}`. **No affiliation-derived auto-flag / `suggested_tier` exists** (D14). |
| `DELETE /api/vouches/invites/{token}` | `require_role()` | subject revokes own open invite |
| `GET /api/coordinator/vouches` | coordinator/admin | **B10.** `?status=contested|self` → `{vouches:[... incl. contested_note, voucher/subject emails]}` — school-scoped |
| `POST /api/vouches/{vouch_id}/resolve` | coordinator/admin | `{action: "verify"|"dismiss"}` → `resolve_vouch(user["school_id"], vouch_id, ...)`; cross-tenant vouch_id → 404 (security C1). (Existing `/verify` route stays for direct tier setting.) |
| `POST /api/orgs/me/contacts` | employer | **C5.** `{display_label, role_title, contact_user_id?}` → contact dict (redaction + own-org membership check per §2.13) |
| `GET /api/orgs/me/contacts` | employer | `{contacts:[...]}` (own org only) |
| `DELETE /api/orgs/me/contacts/{contact_id}` | employer | own-org check → cascade delete (posting_contacts rows too) → `{deleted}` — **the C5 deletion path** |
| `PUT /api/postings/{posting_id}/contact` | employer/coordinator/admin | `_require_own_org_posting`; `{contact_user_id? | employer_contact_id?, relation}` → posting-contact set (pathfinder now targets the real hiring manager; own-org check per security M4) |
| `DELETE /api/postings/{posting_id}/contact` | same | → `{deleted}` (pathfinder falls back to `created_by`) |
| `GET /api/coordinator/orgs` | coordinator/admin | **C5 ERM.** `ErmStore.org_engagement(school)` → `{orgs:[{org_id,name,link_status,postings_live,postings_total,applications,hires,events_attended,last_activity}]}` |
| `DELETE /api/account` | `require_role()` | **A3.** Body `{confirm_email, password}` — `confirm_email` must equal the signed-in email AND `password` must re-verify against the account's PBKDF2 hash (**step-up re-auth for irreversible cross-plane erasure — security L3**; the platform's users have passwords, `accounts.py:72-151`). Runs `erase_account(user["id"])`; response `{erased: true, tables: {...}}`; session is gone (tokens deleted inside the cascade). Employers: org business records survive per §6 policy. |
| `GET /repudiate` (static page; route + exempt-path added in `app.py` in S6) | public | **B3.** Serves `api/static/repudiate.html`. |

### 3.3 Job handlers (registered in `phase5.py`)
- `@register_handler("mentor_match")` — **C4 matcher:** for each under-networked discoverable student (zero shareable edges + `network_analytics` consent — same predicate as C1), rank eligible mentors by program token overlap then least-loaded; `create_offer(origin='matcher', rationale=f"program overlap: {program}")`, max 1 open offer per student; the pair cooldown (§2.13) prevents decline-probing by repeated offers. Enqueued by a coordinator button and by the retention scheduler weekly (`RM_MENTOR_MATCH_HOURS`, default 168, 0 = off). Structural triggers only — the handler never imports `audit_store`/`data_planes` (CI-grep test).
- `build_edges` handler (existing) — now also folds C3/C6 via the new `build_native_edges` internals; check-in and affiliation-confirm routes enqueue it (dedupe-keyed per school, same pattern as consents route platform.py:974).

---

## 4. UI changes per static page

### `api/static/student.html`
- **Notification bell (B4):** header badge polling `GET /api/notifications?unread=1` every 60s; dropdown feed; "mark all read". Deep-link via `entity`/`entity_id`. Titles/bodies rendered through `esc()` (posting titles are employer free-text — security L1).
- **B6 filter bar** above `browseTable`: keyword input, employment-type + work-mode selects, min-pay, deadline-after date, sort select, pager; state → query params.
- **B7:** "Withdraw" button per row in `appsTable` (confirm dialog) for non-terminal apps.
- **B3 pointer:** the network card's disclosure text links to `/repudiate` ("Not a member? Remove yourself…").
- **C6 card "Classes & clubs":** claim form (kind, label, term, role — role labeled "shown to others, self-reported"); my-claims list with confirm state and a **"copy confirm link"** button per claim (share out-of-band with a peer who can attest you — D15); confirm-link landing (hash-param `#affil-confirm=` → shows the claim + Confirm button); claimant list (confirmed viewers only, masked emails); remove-claim.
- **C7 card "Reference ledger":** generate invite link (copy button), open-invite list with revoke; vouch-invite landing (hash-param `#vouch-invite=` → shows subject + structured submit form). "Vouches about me" card gains verify-tier chips.
- **C4:** "I'm an alum" claim (no grad-year field — privacy F2) in profile; mentor panel (visible when `alumni_status='verified'`): mentor profile form + offers inbox with accept/decline.
- **Intro request note** already exists; the intro card shows nothing new for declined (D8).
- All new renders go through the existing `esc()` helper.

### `api/static/employer.html`
- **A9:** replace the lone "Shortlist candidate" button with per-stage actions from the transition map (`applied→shortlist/advance/reject/hire`, `shortlisted→advance/reject/hire`, `advanced→reject/hire`) driving `PATCH /api/applications/{id}`.
- **A10:** "Applicants" view per posting card calling `GET /api/postings/{id}/applications`; email shown only when non-null, else a "not shared" chip; withdrawn rows greyed out.
- **A18:** include `application_email` (the `data-f` attribute already rendered at employer.html:280) in the `collect()` harvest at employer.html:380-396.
- **B8:** "Edit" button on draft/rejected postings re-opens the two-pane extraction-review form pre-filled from `GET /api/postings/{id}`, saving via `PATCH`, then re-submit.
- **B4:** unread-message badge polling the existing `/api/messages/unread-count`; notification bell (shared snippet with student page).
- **C5:** "Contacts" card — add/list/delete org contacts; per-posting "hiring contact" selector (PUT/DELETE posting contact).
- **C3:** "Check in" button on registered events (enter code).

### `api/static/coordinator.html`
- **C1 "Relationship health" card:** under-networked list (`GET /api/coordinator/under-networked`), mentor picker (`GET /api/coordinator/mentors`), per-row "Offer mentor" → `POST /api/coordinator/mentorship-offers` (fire-and-forget; no per-offer status column — D8); aggregate mentorship stats card; coverage numbers from existing `network-coverage`.
- **C2:** intro-outcomes report card (bridged vs organic conversion table + CSV link).
- **B10:** vouch-verification queue (contested + self tabs; Verify / Dismiss; contested note shown).
- **C3:** per-event "Generate check-in code" (renders code big for projection/QR) + roster check-in from the attendees list.
- **C4:** alumni verification queue (approve/deny; no grad_year shown).
- **C5:** ERM table (`GET /api/coordinator/orgs`) with link status + engagement counts; revoke link button already backed by `/api/coordinator/org-links/{id}/revoke`.
- **A1:** repudiation review queue card — asserted name (already redacted+capped at ingest) rendered through **`esc()`** (defense in depth against public→admin stored XSS — security H1; these fields join the escaping test matrix), plus the match-preview line ("matches an active member — approval will NOT touch their data" / "matches N employer-contact rows").
- Notification bell (shared snippet).

### NEW `api/static/repudiate.html` (B3)
Public, no auth, no nav chrome. Explains the right ("someone you know may have uploaded their own contact list; if you're named, you can be removed"), two tabs: **email path** (email → "we sent a confirmation link/token" → token entry → confirm) and **name path** (first/last/company → "queued for review"). Posts to `/api/graph/repudiate` + `/confirm`. Neutral success copy either way (no membership oracle).  Linked from PRIVACY.md and student.html.

---

## 5. Alumni model decision (C4) — detail

**Chosen: `ALTER TABLE users ADD COLUMN alumni_status` via `_COLUMN_UPGRADES` (D4). Rejected: users-table rebuild adding an `'alumni'` role.**

Why the column wins:
1. A rebuild of `users` is the highest-blast-radius operation available (auth root; `tokens`, every store, every JOIN). The 003 consents rebuild was tolerable because consents has 6 columns and no dependents; users does not enjoy that.
2. Role is an *authorization* axis; alumni-ness is an *attribute* axis. A verified alum still browses postings, holds consents, has a profile — i.e. behaves as a student. Mentor surfaces gate on `alumni_status='verified'` (plus employer/coordinator roles), not on a role swap. No `require_role` call sites change.
3. `_COLUMN_UPGRADES` already exists exactly for "users gains a column" and is idempotent across every DB vintage.

**Verification flow (coordinator-attested, no registrar dependency, no grad_year stored — privacy F2):**
1. Student clicks "I'm an alum" (`POST /api/alumni/claim`, empty body) → `alumni_status='self_claimed'`. No new powers. The route hard-codes `user_id=user["id"]` and `status='self_claimed'` (security C2).
2. Coordinator sees the queue (`GET /api/coordinator/alumni`, own school), checks graduation records out-of-band by email (their existing SIS access), clicks Verify/Deny → `verified` / `none` via the school-scoped `set_alumni_status` (security C2). The attestation record is the `events` append-only row (`action='alumni_verified'`).
3. `verified` unlocks: mentor profile creation, `alumni_verified` vouch tier when accepting intros, listing in the C1 mentor picker.
4. Erasure/A3 deletes the user row entirely; nothing alumni-specific survives.
5. `alumni_status` is barred from every scoring feature dict via `NO_SCORING_ATTRIBUTE_KEYS` (§1); `grad_year` sits in `PROTECTED_KEYS` so no future column or feature can carry it without tripping CI.

---

## 6. Erasure cascade (A3) — `stores/erasure.py` + operator script

### Ordering & transaction strategy
Cross-DB atomicity between `platform.db` and `audit.db` is impossible (two SQLite files, never one connection — boundary #2 forbids even trying). Strategy (D12), idempotent at every step so a crash anywhere is fixed by re-running:

**Phase 0 (platform.db, autocommit):** `DELETE FROM tokens WHERE user_id=?` — immediate session kill, so nothing else races the cascade.
**Phase 1 (audit.db):** `AuditDB().delete_self_id(f"student-{uid}")`. If this fails, abort — the account survives intact and the user/operator retries.
**Phase 2 (platform.db, ONE transaction):** table-by-table, then tombstone, then the users row, then commit.

### Phase-2 deletion order (single `BEGIN IMMEDIATE` transaction)
No FK constraints exist between these tables, so order is chosen for auditability (children before parents):

| # | Table | Statement |
|---|---|---|
| 1 | `notifications` | `WHERE user_id=?` |
| 2 | `intro_requests` | `WHERE requester_user_id=? OR target_user_id=? OR broker_user_id=?`; **plus (privacy F8): scan surviving rows' `path_json` in Python and DELETE any request whose path nodes include the erased uid** (an intermediate hop on a >2-hop path is not covered by the principal columns; the request is meaningless without its full path) |
| 3 | `vouches` | `WHERE voucher_user_id=? OR subject_user_id=?` |
| 4 | `vouch_invites` | `WHERE subject_user_id=?`; plus `UPDATE vouch_invites SET used_by=NULL WHERE used_by=?` |
| 5 | `graph_edges` | `WHERE user_a=? OR user_b=?` |
| 6 | `member_graph_identity` | `WHERE user_id=?` |
| 7 | `broker_blocks` | `WHERE broker_user_id=? OR blocked_user_id=?` (A17) |
| 8 | `mentor_profiles` | `WHERE user_id=?` |
| 9 | `mentorship_offers` | `WHERE student_user_id=? OR mentor_user_id=?` |
| 10 | `affiliation_claims` | `WHERE user_id=?`; plus `UPDATE affiliation_claims SET confirmed_by=NULL WHERE confirmed_by=?` (peer's own claim survives; the attestation link is anonymized; the fold never re-mints an edge to a deleted uid) |
| 11 | `event_checkins` / `event_registrations` | `WHERE user_id=?` |
| 12 | `messages` | `WHERE sender_user_id=? OR application_id IN (SELECT id FROM applications WHERE student_id=?)` |
| 13 | `interview_slots` | `WHERE application_id IN (user's applications) OR proposed_by=?` |
| 14 | `applications` | `WHERE student_id=?` |
| 15 | `match_results` | `WHERE student_id=?` |
| 16 | `resumes` | `WHERE user_id=?` (blob + text, hard) |
| 17 | `student_profiles` | `WHERE user_id=?` |
| 18 | `projects` | `WHERE user_id=?` |
| 19 | `posting_contacts` | `DELETE WHERE contact_user_id=?`; `UPDATE ... SET added_by=0 WHERE added_by=?` |
| 20 | `employer_contacts` | `DELETE WHERE contact_user_id=?`; `UPDATE ... SET added_by=0 WHERE added_by=?` (C5 erasure hook) |
| 21 | `repudiation_requests` | `UPDATE SET decided_by=NULL WHERE decided_by=?` |
| 22 | `jobs` | `DELETE WHERE owner_user_id=? AND status IN ('queued','running')`; `UPDATE jobs SET payload_json='{}', result_json=NULL, owner_user_id=NULL WHERE owner_user_id=?` (finished rows keep only anonymous shape) |
| 23 | `consents` | `DELETE WHERE user_id=?` — **policy:** the suppression tombstone + an anonymized `events` row are the durable erasure proof; retaining per-purpose consent history for a deleted person is itself retained PII. Documented in PRIVACY.md. |
| 24 | append-only logs — **anonymize, never delete** (audit-retention basis documented): `UPDATE events SET actor_user_id=NULL WHERE actor_user_id=?`; **plus (privacy F8): `UPDATE events SET entity_id=NULL WHERE entity='user' AND entity_id=CAST(? AS TEXT)`** — the erased person as the *subject* of a logged action (e.g. `alumni_verified`) must not survive as a re-linkable id; `UPDATE posting_events SET actor_user_id=NULL WHERE actor_user_id=?`; `UPDATE intro_events SET actor_user_id=NULL WHERE actor_user_id=?` (rows already free-text-free by design) |
| 25 | `graph_suppressions` | `INSERT (school_id, user_id, reason='member_deleted', created_at)` — inside the txn, so a partially-visible state can never re-materialize edges |
| 26 | **employer business records policy:** `postings` are org records, not personal data → postings survive; `UPDATE postings SET created_by=0 WHERE created_by=?` (0 = "erased user" sentinel; `_notify_creator` already no-ops on missing user; **all edge folds carry `AND p.created_by != 0` and `_hiring_manager` maps 0 → None — feasibility M5**). If the erased employer is the org's only member, live postings are transitioned to `closed` first (no orphaned live postings). Coordinator-reviewed decisions (`reviewed_by`) anonymized to NULL. |
| 27 | `users` | `DELETE WHERE id=?` |
| — | `COMMIT` | |

**Note:** `delete_my_network(reason='member_deleted')` is NOT called as a sub-step (it opens its own connection/txn); its statements are inlined above (5, 6, 7, 2, 3) so the whole platform-plane cascade is one atomic transaction.

### Operator DSR script — NEW `scripts/dsr_erase.py`
```
python scripts/dsr_erase.py --email who@x.com [--user-id N] [--dry-run] [--json]
```
Resolves the user, prints the table-by-table counts from `erase_account(dry_run=True)`, asks for `ERASE <email>` confirmation (skippable with `--yes` for scripted DSRs), executes, prints the receipt `{user_id_hash, erased_at, tables}` for the DSR file. Exit 0 only when both planes report success. Also supports `--repudiate --school N --email/--name` for the non-member path (calls the same store methods as the API, including the F3 member-refusal rule).
`DELETE /api/account` (self-serve) and the script share `erase_account` — one implementation.

**Docs fix folded in:** RELATIONSHIPS.md:433 / IMPLEMENTATION.md:431 currently overclaim the cascade exists; slice S4 updates both to point at `stores/erasure.py` as the implementation, and documents the C1 roster under the governed positive-action program (monitoring + shut-off) per RELATIONSHIPS.md Slice AI (privacy F7).

---

## 7. Slice plan (9 restartable slices; file-disjoint within each parallel wave)

**Dependency shape:** S1 → (S2, S3, S4 in parallel) → (S5, S6 in parallel) → (S7, S8, S9 in parallel).

Cross-wave file overlaps (allowed — waves are sequential): `tests/test_phase4_intros.py` in S2 then S5; `tests/test_platform_reports.py` in S3 then S5 (feasibility H2 — the `aggregate()`-pinned expectations MUST be updated in S3 so S3 ends green; S5 later adds route-level assertions); `api/app.py`/`api/auth.py` in S4 then S6 (feasibility M3 — the phase5 router mount, its `_PLATFORM_PREFIXES` entries, `/api/account`, and the `/repudiate` exempt-path all land in S6, since `api/phase5.py` does not exist before S6).

### S1 — Migration 004 + store foundations *(wave 1; everything depends on this)*
**Files:** `resume_matcher/stores/migrations/004_phase5.sql` (new), `resume_matcher/stores/db.py` (`_COLUMN_UPGRADES`: users.alumni_status, intro_requests.origin, campus_events.checkin_code), `resume_matcher/stores/data_planes.py` (grad_year → `PROTECTED_KEYS`; `NO_SCORING_ATTRIBUTE_KEYS` + `assert_no_protected` third check), `resume_matcher/stores/students.py` (B7 transitions + `withdraw` + `filter_by_consent` + school-scoped alumni helpers), `resume_matcher/stores/notifications.py` (new), `resume_matcher/stores/retention.py` (purge additions incl. name_review TTL scrub + admin_sessions sweep), `tests/test_phase5_migration.py` (new), `tests/test_platform_students.py` (additions), `tests/test_platform_db.py` (additions).
**Key tests:** 004 applies on fresh + **populated** DB (pre-seeded graph_edges/applications rows; row counts + `PRAGMA index_list` after rebuild); partial re-run does not wedge (v2 scratch drop + no raw ALTERs); twice-guard via schema_version; A14 cleanup revokes exactly non-accepted-slot edges and is idempotent; withdrawn transition matrix (incl. employer-can't-withdraw guard); alumni_status/origin/checkin_code columns on legacy/bare tables; notification CRUD + purge windows + user-scoped mark_read + **no-email-in-title/body invariant**; `test_platform_db.py` green with no allowlist edit; `assert_no_protected` rejects `alumni_status`/`grad_year` in a feature dict.

### S2 — Graph-store integrity + new edge folds *(wave 2; parallel with S3/S4)*
**Files:** `resume_matcher/stores/relationships.py` (D16 `seen_at`, A13, A14, C3/C6 folds, EDGE_STRENGTH, B10 store methods), `resume_matcher/stores/graph.py` (A17, A1 store half: split executors + queue + caps), `resume_matcher/stores/intros.py` (A2 `broker_ok` + in-txn consent check, C2 origin + `outcome_rows`), `resume_matcher/stores/phase5.py` (new: Mentor/Affiliation/VouchInvite/Erm/Contact stores), `resume_matcher/stores/engage.py` (C3 check-in methods, school-scoped), `tests/test_phase4_graph.py`, `tests/test_phase4_intros.py`, `tests/test_phase5_stores.py` (new).
**Key tests:** `test_backfill_ignores_preconsent_interactions` **end-to-end** (interact → grant → run build_edges job → still pending — feasibility H1); accepted-only interview fold; peer_coattendance: cap respected, never folds from RSVP, set-based fold preserves suppression-skip + never-resurrects-revoked, watermark skips unchanged events; **affiliation edges mint ONLY between attestation pairs — two colluders on a 50-claim affiliation gain exactly one edge, between themselves** (security H2); confirm rules (self-confirm rejected, non-claimant rejected, bootstrap reciprocity, daily cap); broker without warm_intro invisible and shape-identical to no-path; consent revoked mid-create aborts inside the txn (security M2); email-path repudiation deletes self_upload edges only; **name-path repudiation never touches an active member's rows and inserts no member-scoped suppression** (privacy F3); per-email + global challenge caps (security H3); name fields redacted + capped at ingest (security H1); broker_blocks cleared; mentor eligibility matrix; create_offer cross-school 404 (security C1); pair cooldown; invite lifecycle: hashed at rest, cross-school consume rejected (security M1), caps; contact redaction + own-org membership check (privacy F5 / security M4).

### S3 — Fairness/audit integrity (A4, A8, A11, A12) *(wave 2; disjoint from S2/S4)*
**Files:** `resume_matcher/audit/metrics.py`, `resume_matcher/audit/compliance_pack.py`, `resume_matcher/audit/proxy_leakage.py`, `resume_matcher/stores/audit_store.py`, `resume_matcher/inference/adapters/mock.py`, `tests/test_audit.py`, `tests/test_compliance_pack.py`, `tests/test_fairness_gate.py`, `tests/test_adapter_contract.py`, `tests/test_phase4_fairness.py` (A4 expectation flip — feasibility H2), `tests/test_platform_reports.py` (the two `aggregate()`-pinned expectations: responses==9 → banded/floored; responses==5 → below cohort floor — feasibility H2).
**Key tests:** zero-access group can never PASS (bounded-suppression matrix: pass/fail/indeterminate — the `access_disparity({"a":6},{"a":10,"b":10})` case now pins **False**, not None); single-group → None; zero-selection → None with note; banded responses under partial suppression; cohort floor (< 2·MIN_CELL → no exact total); snapshot pinning (same payload until age+delta thresholds); midrank AUC vs known ties; intercept present after standardization; `method` field honesty; 'JS' alias earns credit with no fabrication flag.

### S4 — Auth, erasure, deploy/docs (A3, A15, A16) *(wave 2; disjoint from S2/S3)*
**Files:** `resume_matcher/api/auth.py` (admin sessions + pw_fingerprint + prod assert; NOT the phase5 prefixes — those are S6), `resume_matcher/api/app.py` (admin-session login/logout wiring only), `resume_matcher/api/accounts.py` (`alumni_status` in user dict), `resume_matcher/stores/erasure.py` (new), `scripts/dsr_erase.py` (new), `scripts/deploy.sh`, `scripts/deploy.ps1` (prod notes — feasibility L6), `deploy/cohost/.env.example`, `docs/RELATIONSHIPS.md` + `docs/IMPLEMENTATION.md` (overclaim fixes, C1 governed-program documentation, out-of-scope gates), `tests/test_accounts.py`, `tests/test_phase5_erasure.py` (new), `tests/test_require_role.py`.
**Key tests:** logout invalidates server-side; expired admin session rejected; old HMAC-style cookie rejected; **password rotation invalidates all outstanding sessions (pw_fingerprint — security M5)**; prod boot refuses weak/unset password + insecure-cookie combo, dev boots; erasure leaves zero rows across all touched tables (parameterized sweep), append-only logs anonymized not deleted **including `entity_id` user refs and surviving path_json hops (privacy F8)**, tombstone present, employer postings survive with sentinel, dry-run touches nothing, double-run idempotent; audit-plane self-ID gone.

### S5 — A-item API enforcement + B6/B7 in `platform.py` *(wave 3; after S2+S3)*
**Files:** `resume_matcher/api/platform.py` (all §3.1 rows), `resume_matcher/stores/platform.py` (`PostingStore.search`), `tests/test_platform_api.py`, `tests/test_platform_reports.py` (route-level: A7 cohort filters incl. network-coverage, snapshot serving), `tests/test_phase4_intros.py` (route-level A2).
**Key tests:** repudiation 202-shaped both paths + rate-limited (two-arg `.allow`); coordinator decide school-scoped → cross-tenant 404 (security C1); consent-revoke cascades (A5 blob gone + matches gone; A6 audit row gone); A7 applied to intro-equity AND network-coverage, NOT self-id (feasibility H3); snapshot serving; B6 search/filter/sort/pagination + `%`/`_` LIKE-escape (security L4); B7 withdraw route + funnel exclusion; `_hiring_manager` sentinel-0 → 409 not uid-0 target (feasibility M5); evidence-card exposure logged once.

### S6 — Phase-5 API surface *(wave 3; parallel with S5; after S2+S4)*
**Files:** `resume_matcher/api/phase5.py` (new), `resume_matcher/api/app.py` (phase5 router mount, `/repudiate` static route + exempt path), `resume_matcher/api/auth.py` (`_PLATFORM_PREFIXES` additions incl. exact `/api/account` — feasibility M2/M3), `tests/test_phase5_api.py` (new).
**Key tests:** every §3.2 route: auth matrix (401/403/404-not-leak) + **parameterized cross-tenant test per coordinator route (security C1/C2)**; notifications feed/read user-scoping; mentorship double-opt-in incl. mentor-only respond, silent decline, **coordinator surface exposes zero per-offer status (privacy F9)**; alumni claim (no grad_year anywhere in request/response/queue — privacy F2), verify flow + cross-school 404, student cannot self-verify; under-networked: analytics-consent filter, no self-ID field, no grad_year, **access-log row written per read (privacy F7)**; intro-outcomes MIN_CELL; check-in code rate limit + roster school checks (security M3); affiliation claim/confirm-link/delete + **unconfirmed-viewer claimants 404 + masked emails (privacy F1)**; vouch invites end-to-end: token never at rest, cross-school 404, `redact_text` on submit (security M6), **no suggested_tier in any response (D14)**; C5 contact CRUD + cascade delete + `_hiring_manager` preference order; ERM rollup; `DELETE /api/account` requires password step-up (security L3) and 401s without the `/api/account` prefix fix; `/repudiate` publicly reachable.

### S7 — Student + public UI *(wave 4; parallel with S8/S9)*
**Files:** `resume_matcher/api/static/student.html`, `resume_matcher/api/static/repudiate.html` (new).
**Key tests:** route smoke (`/repudiate` public, no admin redirect), escaping checks per new surface (existing pattern in `tests/test_platform_api.py` UI smoke section).

### S8 — Employer UI (A9, A10, A18, B8, B4-badge, C3, C5) *(wave 4)*
**Files:** `resume_matcher/api/static/employer.html`.
**Key tests:** smoke + escaping; A18 regression (submitted posting carries application_email).

### S9 — Coordinator UI (C1, C2, C3, C4, C5, B10, A1-queue) *(wave 4)*
**Files:** `resume_matcher/api/static/coordinator.html`.
**Key tests:** smoke + escaping — **the repudiation queue card's first/last/company render is in the escaping matrix (security H1)**.

Every slice ends green under `RM_PLATFORM_ENABLED=1 pytest`; slices are restartable (each is a self-contained commit; 004 is version-gated so re-runs are no-ops).

---

## 8. Consent / privacy analysis per new feature

| Feature | Write path gated by | Read/traversal gated by | Notes |
|---|---|---|---|
| **Notifications (B4)** | server-side only (no user writes) | own rows only (`user_id=me`) | No user free text stored; titles composed from org/posting content, never user emails; intro-decline and mentorship-decline emit nothing (D8). Retention-purged. |
| **Posting search (B6)** | n/a | student sees `live` + own-school only (existing `_can_view` school scope) | No new data class. LIKE metacharacters escaped. |
| **Withdrawal (B7)** | student owns the application | employer sees status change (they already saw the application) | Withdrawal reason is never collected. Funnel excludes withdrawn. |
| **Posting edit (B8)** | `_require_own_org_posting` + draft/rejected 409 guard (existing) | unchanged | — |
| **Vouch queue (B10)** | coordinator role, own school (D13) | coordinator sees contested note (subject wrote it *to* be reviewed) | Contested vouch stays out of traversal until resolved (existing). |
| **Repudiation page (B3/A1)** | public, IP-rate-limited + per-email/global send caps, challenge or human review | neutral 202 shapes — no membership oracle either path | Email stored only hashed; name held redacted + capped, only while pending, scrubbed at decision AND at 30-day TTL. Email path (proved control) may delete the prover's own derived data; **name path never touches an active member and never inserts a member-scoped suppression** — matched members get a `repudiation_notice` pointing at their own controls (privacy F3). Name path deletes matching employer-contact rows (privacy F5 match path). |
| **C1 under-networked** | — | coordinator/admin, own school; cohort = `graph_discoverable` ∩ `network_analytics` (decision per feasibility H3: an individual-level roster is analytics about the person); **every read access-logged** (privacy F7) | Trigger is purely structural (zero shareable edges); never touches audit.db; no self-ID field and no grad_year by construction (test-enforced). Governed positive-action framing per RELATIONSHIPS.md Slice AI; documented shut-off. |
| **C2 intro outcomes** | — | coordinator/admin; cohort ∩ `network_analytics` (A7); MIN_CELL=5 every cell; snapshot-served (A8) | origin is per-request metadata, not per-person attribute; report is aggregate-only. |
| **C3 check-ins / peer edges** | self check-in requires the student's own action (code + registration, rate-limited) or coordinator roster (school-checked); both imply presence-attestation | edges default `pending`; traversable only when BOTH hold `graph_discoverable` (unchanged `_SHAREABLE`) | Consent architecture untouched; provenance quote = event title (org content). Cap prevents fair-sized cliques; set-based fold bounds rebuild cost. RSVP alone never mints a peer edge. |
| **C4 alumni + mentorship** | mentor_profiles row = explicit opt-in (D1); matching also requires live `warm_intro` + `graph_discoverable`; offer accepted only by the mentor | mentorship edge under `_SHAREABLE`; student identity revealed to mentor only at offer time | **Asymmetry stated explicitly (feasibility L8): the double-opt-in is mentor-side only** — a matcher/coordinator-origin offer discloses the student's identity+program to the chosen mentor on the strength of the student's standing `graph_discoverable`, matching the shipped `coordinator_bridge` precedent; an accepted offer mints a *pending* edge that still needs both-endpoint consent to traverse. No grad_year exists anywhere. Decline silent to student and invisible to coordinators (D8/privacy F9); coordinators see MIN_CELL'd aggregates only. |
| **C5 ERM / contacts** | employer adds own org's business contact (PIPEDA business-contact exemption, per 003 design); **redact_text() + escape + CSV-neutralize at write** — role/title text only can survive (privacy F5); `contact_user_id` must be an own-org member (security M4) | coordinator ERM view is org-level counts only, zero student identities | Deletion paths: employer cascade-delete endpoint; erasure hook (`contact_user_id` rows die with the person); **name-path repudiation deletes matching free-text contact rows** (privacy F5). Documented in PRIVACY.md. |
| **C6 affiliations** | self-asserted claim (self-disclosure by definition); **attestation-pair confirmation via confirm-links** before any edge (D15) | **claimant list requires a CONFIRMED claim and shows confirmed co-claimants only, emails masked** (privacy F1); edges pending until both endpoints hold `graph_discoverable`; `expires_at` 12mo | No registrar data touched (FIPPA indirect-collection avoided entirely); enumeration bounded by claim cap + confirmed-viewer gate + zero visibility for unconfirmed claims; no confirm-broadcast notifications; removing a claim hard-deletes derived edges. `claim_role` is display-only — it feeds no tier, flag, or authorization (D14). |
| **C7 vouch invites** | subject generates the link (consent to be vouched-about is the act of asking); voucher must sign in (accountable identity) AND belong to the invite's school (security M1) | evidence + relationship `redact_text()`-ed in the submit handler (security M6); subject can view + contest (existing) | Invite-by-link kills the member-search oracle (D10); tokens hashed at rest; invites expire + revocable; `used_by` anonymized on voucher erasure. |
| **Erasure (A3)** | account owner (password step-up — security L3) or operator DSR | — | Two-plane ordered cascade (§6); tombstone inside txn; append-only logs anonymized including subject-side `entity_id` refs; surviving path_json hops scrubbed (privacy F8); consent rows deleted per policy note. |
| **Admin sessions (A15/A16)** | — | — | Server-side random tokens, hashed at rest, bound to the password fingerprint (rotation = logout-all — security M5); logout real; prod refuses weak/unset password; expired rows swept. No PII. |

**Boundary re-verification:** no Phase-5 table/column collides with `PROTECTED_KEYS` or `NETWORK_FEATURE_KEYS`; `grad_year` is now *in* `PROTECTED_KEYS` and no such column exists; `alumni_status` is barred from feature dicts via `NO_SCORING_ATTRIBUTE_KEYS`; no new code path joins graph data into `match_results` or any scoring feature dict (CI test unchanged and still green); every new traversal read goes through `_SHAREABLE`; MIN_CELL=5 on every new aggregate egress (C2 report, mentorship stats; ERM is org-level counts not person-level); no LinkedIn-adjacent ingestion added; no fabricated probability anywhere (mentor match rationale is a string of structural facts).

---

## 9. Adversarial findings folded in as hard requirements

Each finding below is a **hard requirement** of this spec; its required fix is binding on the implementing slice (named in brackets). IDs: P=privacy review, SC/SH/SM/SL=security review (critical/high/med/low), FH/FM/FL=feasibility review.

### CRITICAL / HIGH (build-blocking)
| ID | Finding | Required fix (where it landed) |
|---|---|---|
| P-F1 (CRIT) | Affiliation claimant list = mass email-enumeration oracle (any-status viewer gate + emails + confirm-broadcast) | `claimants` requires a **confirmed** claim, returns confirmed co-claimants only, emails masked; full email only inside an attestation pair; confirmation is confirm-link-directed; the confirm-request broadcast notification is removed; test: unconfirmed claimant on a 50-member affiliation → 404 (§2.13, §2.11, §3.2; S2/S6) |
| SC-C1 (CRIT) | Coordinator store methods unscoped by school → cross-tenant mutation (decide_repudiation, resolve_vouch, mentorship-offers, roster check-in) | D13: every coordinator read/mutation takes `school_id` from `user["school_id"]` with `AND school_id=?`; cross-tenant ids → 404; parameterized cross-tenant test per route (§2.1, §2.2, §2.5, §2.13, §3.2; S2/S5/S6) |
| SC-C2 (CRIT) | `set_alumni_status` unscoped → cross-tenant privilege escalation; self-claim could self-verify | `set_alumni_status(user_id, school_id, ...)`; verify route 404s cross-school; claim route hard-codes `user_id=user["id"]`, `status='self_claimed'` (§2.4, §3.2, §5; S1/S6) |
| P-F2 (HIGH) | `grad_year` = age proxy entering the scoring plane unguarded | grad_year is **never collected or stored**; added to `PROTECTED_KEYS` (CI trips on any future column/feature); `alumni_status` barred from feature dicts via `NO_SCORING_ATTRIBUTE_KEYS`; removed from alumni claim/queue and the C1 roster (§1, §3.2, §5; S1/S6) |
| P-F3 (HIGH) | Name-review repudiation deletes a member's graph on an unauthenticated third-party assertion, silently | Split executors: only the email path (proved address control) may touch member data; `repudiate_execute_name` refuses any token matching an active member, inserts **no** member-scoped suppression, and notifies the member (`repudiation_notice`) instead (§2.2, §8; S2) |
| P-F4 (HIGH) + SH-H2-role | Self-asserted ta/instructor + bootstrap manufactures coordinator-tier vouch authority | D14: `claim_role` is display-only; `has_confirmed_role` and the `suggested_tier` auto-flag are **removed** from Phase 5; no code path branches on claim_role (§2.13, §3.2; S2/S6) |
| P-F5 (HIGH) | Employer contact free text = unredacted third-party PII with no removal path | `display_label`/`role_title` length-capped + `redact_text()` at write (role/title text only survives); free-text-contact policy documented; name-path repudiation deletes matching contact rows (§2.13, §2.2; S2) |
| SH-H1 (HIGH) | Public→admin stored XSS via repudiation first/last/company rendered in the coordinator card | Length-cap 80 + `redact_text()` at ingest in `create_repudiation`; coordinator card renders through `esc()`; fields added to the escaping test matrix (§2.2, §4, S9) |
| SH-H2 (HIGH) | Affiliation fold mints a clique from one colluding confirmation | D15: edges fold only along attestation pairs (confirmer ↔ confirmed claimant); two colluders gain exactly one edge between themselves; per-user daily confirm cap (§2.1, §2.13; S2) |
| SH-H3 (HIGH) | Email-bombing via the challenge path (per-IP limiter spoofable via XFF) | IP-independent caps in the store: ≤ `RM_REPUDIATE_MAX_PER_EMAIL` (3)/24h per email_hash + global `RM_REPUDIATE_MAX_EMAILS_PER_DAY` (50); silent same-shape 202 when capped; XFF trust boundary documented at the limiter (§2.2, §3.1; S2/S5) |
| FH-H1 (HIGH) | A13 pre-consent guard is a no-op (every rebuild bumps `last_seen_at`; grant itself triggers a rebuild) | D16: `upsert_edge(seen_at=...)`; folds pass source interaction timestamps; `last_seen_at` advances only with the source; end-to-end test through the build_edges job (§2.1; S2) |
| FH-H2 (HIGH) | A8/A4 break pinned tests (`responses==9`, `responses==5`, fairness `None`→`False`) and the fixes sat in the wrong slices | `tests/test_phase4_fairness.py` and the two `test_platform_reports.py` aggregate expectations moved into S3 (same slice as the behavior change); expected values re-derived in §7 S3 (S3) |
| FH-H3 (HIGH) | A7 over-applied (would empty the self-id report) and under-applied (network-coverage never filtered) | Self-id report keeps `self_id_audit` as its basis; `network_analytics` filter added to network-coverage aggregates AND intro-equity; C1 roster decision made explicit (requires `network_analytics`) (§3.1, §3.2, §8; S5/S6) |

### MEDIUM (applied)
- **P-F6** — name_review rows get a 30-day TTL (`expires_at`) with scrub-on-expiry in `run_retention`, independent of coordinator action (§1, §2.15).
- **P-F7** — under-networked roster: access-logged per read, grad_year removed, no-self-ID test kept, documented under the governed-program shut-off (§3.2, §6 docs note).
- **P-F8** — erasure also nulls `events.entity_id` for `entity='user'` and scrubs/deletes surviving `path_json` hops referencing the erased uid (§6 steps 2, 24).
- **P-F9** — mentorship decline is invisible to coordinators: no per-offer status surface; aggregates only, MIN_CELL'd; neutral create_offer responses + pair cooldown (D8, §2.13, §3.2).
- **SM-M1** — vouch-invite consumption rejects cross-school vouchers; vouch + edge always in the invite's school (decided: reject, not remap) (§2.13).
- **SM-M2** — broker `warm_intro` final check moved inside `IntroStore.create`'s transaction (§2.3).
- **SM-M3** — check-in-by-code rate-limited per user + IP; code is a second factor on top of required registration; roster check-in school-checked (§2.5, §3.2).
- **SM-M4** — `contact_user_id` must be a member of the employer's own org (or NULL) in both `add_contact` and `set_posting_contact` (§2.13).
- **SM-M5** — admin sessions bound to a password fingerprint (rotation = logout-all); expired-row sweep in retention; cookie flags asserted (§1, §2.15, §2.16).
- **SM-M6** — invite-submit `evidence`/`relationship` pass through `redact_text()` in the handler (§3.2).
- **FM-M1** — 004 made re-run tolerant: scratch-table drops up front; both raw ALTERs moved to `_COLUMN_UPGRADES`; "process-locked" claim corrected (§1).
- **FM-M2** — exact `/api/account` added to `_PLATFORM_PREFIXES` (in S6) so `DELETE /api/account` isn't 401'd by the admin gate (§2.16, §3.2).
- **FM-M3** — phase5 router mount + prefix/exempt additions moved from S4 to S6 (`api/phase5.py` exists by then); S4 keeps only admin-session app wiring (§7).
- **FM-M4** — peer fold is one set-based upsert per event with a new-check-ins watermark; cost stated (≤11 175 rows/statement, zero steady-state) (§2.1, D9).
- **FM-M5** — folds exclude `created_by=0`; `_hiring_manager` maps sentinel 0 → None (§2.1, §3.1, §6 step 26).

### LOW (applied)
- **SL-L1** — notifications feed/mark_read carry `WHERE user_id=?`; bell renders esc()'d (§2.11, §4).
- **SL-L2** — executors reachable only from `confirm_repudiation`/`decide_repudiation` (test); token derivation pinned per path (email path from confirmed email only); coordinator queue shows a non-identifying match preview before approval (§2.2).
- **SL-L3** — `DELETE /api/account` requires password step-up (PBKDF2 re-verify; platform users have passwords) (§3.2).
- **SL-L4** — B6 `LIKE` uses `ESCAPE '\'` and escapes `%`, `_`, `\` (§2.14).
- **FL-L1** — mentorship_offers table UNIQUE replaced by a partial unique index on open offers; re-offer after terminal allowed post-cooldown (§1).
- **FL-L2** — vouch-invite tokens stored as sha256 only; school-scoped lookups (§1, §2.13).
- **FL-L3** — `remove_claim` hard-deletes derived edges (stated; allows genuine re-claim to re-mint) (§2.13).
- **FL-L4** — test: composed notification titles/bodies contain no user emails (§2.11, S1).
- **FL-L5** — `_client_key` duplicated locally in platform.py; `.allow(key, now)` two-arg call (§3.1).
- **FL-L6** — `scripts/deploy.sh` AND `scripts/deploy.ps1` in S4's file list with the prod note (§7 S4).
- **FL-L7** — D3 wording corrected: one explicit index + inline UNIQUE (§0).
- **FL-L8** — C4 mentor-side-only double-opt-in asymmetry stated explicitly in §8.
- Feasibility "verified clean" corrections adopted: `taxonomy.surface_forms` is already public — the draft's re-export note is dropped and `matching/taxonomy.py` leaves S3's file list; S1 migration tests include the populated-upgrade case.

### Rejected / partially rejected (with reasons)
1. **SH-H2 sub-recommendation "drop or tightly cap the two-party bootstrap"** — bootstrap **kept** (with the daily confirm cap): under D15's pair-only folding, colluders can only mint an edge *between themselves*, which is true information; removing the bootstrap would make every affiliation's first confirmation impossible without coordinator involvement.
2. **P-F4 alternative "authority claim_roles gain coordinator attestation"** — rejected in favor of **removing the authority consumer entirely** (D14: no auto-flag, claim_role display-only); building a coordinator attestation subsystem for course roles adds surface for a convenience the vouch queue never needed.
3. **SL-L2 sub-recommendation "coordinator sees exactly which records the name path will delete"** — folded as **counts + booleans only** (member_matched / contact_matches), not row-level listing: enumerating the matched member rows to a coordinator acting on an unauthenticated third-party assertion would itself disclose membership — the same oracle class the neutral 202 shapes exist to prevent.
