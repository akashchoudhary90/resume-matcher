# Platform architecture — the Handshake replacement

**Status: approved direction (2026-07-12).** This document is the architecture for evolving the
Resume Matcher into a full campus-recruiting platform for York career services: employers post
jobs, students browse/apply with consented profiles, coordinators approve postings and read the
bias audit — with the existing matching engine ranking both directions, and a flagship
**JD-upload → auto-filled posting** flow (spec: [`JD_AUTOFILL.md`](JD_AUTOFILL.md)) that removes
the manual form-filling Handshake still requires.

The four load-bearing boundaries in [`DESIGN.md`](DESIGN.md) are unchanged and this design is
shaped around keeping them enforceable at platform scale.

---

## How this design was chosen

Three competing architectures were drafted and judged against the codebase (fit with existing
code, time to MVP, evolvability, boundary compliance):

- **A — MVP-first:** evolve the FastAPI monolith in place; SQLite; one container; ship the
  flagship in weeks.
- **B — Platform-first:** Postgres + Alembic + outbox + RLS + object storage + multi-school
  tenancy from day one.
- **C — Compliance-first:** two physically separate databases, consent lifecycle, decision/
  exposure logging, AEDT/Ontario-WFWA obligations mapped to code.

**Winner: A**, with a specific graft list from B and C (below). B front-loads 4–5 new operational
systems before the first posting goes live — wrong bill for a ~1-dev team. C is the compliance
*target state*, not the path: its consent/encryption/exposure machinery is load-bearing before an
employer can post a job. A is the only plan whose week-level estimates are credible, and its
seams (`stores/db.py`, `workers/runner.py`) isolate the later Postgres/queue port.

**Grafts adopted from B (cheap now, expensive later):**

1. `school_id` column on every core table from migration 001, York hardcoded as the only row.
   Retrofitting tenancy touches every query; a column now costs nothing.
2. Employer approval is an **`employer_school_links` row** (org × school × status × reviewed_by),
   not a boolean `org_verified` — that is the actual Handshake-shaped relationship.
3. A first-class **append-only `consents` table** (purpose, version, granted_at, revoked_at)
   instead of a `consent_version` string on resumes. `resume_storage` and `profile_matching` are
   separate grants; the match pool filter applies **before** retrieval.
4. CI-pin `job_posting.schema.json` and `score_result.schema.json` alongside the existing
   `match_extraction.schema.json` pin — the engine's contract seam, for free.
5. `jobs` table carries `attempts`, `run_after`, `locked_by`, and an idempotency/dedupe key from
   day one. Claim = `UPDATE … WHERE status='queued'` in SQLite; ports trivially to
   `SKIP LOCKED` later. Retry-with-backoff is painful to retrofit.

**Grafts adopted from C (the compliance spine):**

6. `score_kind` is a **NOT NULL column with a CHECK** on `match_results`
   (`fit_readiness_not_hire_probability`) — boundary #4 schema-enforced, not convention.
7. **Exposure events:** the first time a human views a shortlist, append a defense-file
   hash-chain entry (who, posting, candidate refs, rank order, timestamp) — reuses
   `audit/defense_file.py`; this is the AEDT-relevant moment and the cheapest compliance
   differentiator anywhere in the bake-off. Batched per (viewer, posting) to bound volume.
8. **Ontario Working-for-Workers AI-disclosure block** auto-inserted into every published
   posting ("AI-assisted screening is used…"). Mandatory, not employer-optional.
9. Employer JD text goes through the **same adapter locality gate** as resumes: non-local
   adapters only ever see a redacted copy (`assert_redacted` tripwire, see JD_AUTOFILL.md §P0).
10. **Append-only `posting_events`** transition log for the approval state machine.
11. `audit/proxy_leakage.py` becomes a **CI gate on migrations**: no scoring-visible column may
    be added that names or proxies a protected attribute (plus the mechanical
    `PROTECTED_KEYS`-column grep test).
12. A `human_review_requested` flag on applications (student can ask a human to look).

**Dissent worth recording:** the privacy-officer judge preferred C outright, scoring A's
email+password pilot posture as the thinnest FIPPA reading. Mitigation is procedural, not
architectural: **validate the FIPPA/PIA/SSO sequencing with York before building Phase 2** — SSO
may turn out to be a phase-0 gate for any real-student pilot, and it slots into the auth layer
without redesign (only how a `users` row gets created changes).

---

## Component diagram

```
                        ┌────────────────────────────────────────────────────────────┐
                        │  Caddy (TLS)  — deploy/cohost/Caddyfile (unchanged)        │
                        └───────────────────────────┬────────────────────────────────┘
                                                    │ :8000 (internal)
┌───────────────────────────────────────────────────▼───────────────────────────────────────────┐
│  FastAPI monolith — resume_matcher/api/app.py  create_app()          (single uvicorn worker)  │
│                                                                                                │
│  ROUTERS (new, split out of app.py)                 STATIC UI (vanilla HTML, like demo.html)   │
│   api/routers/postings.py    ─ JD autofill + CRUD    static/employer.html   (post + review)    │
│   api/routers/students.py    ─ profile/resume/apply  static/student.html    (browse/apply/fit) │
│   api/routers/coordinator.py ─ approval queue, audit static/coordinator.html (queue + audit)   │
│   api/routers/matches.py     ─ shortlists, coaching  static/login.html      (reused)           │
│   api/routers/account.py     ─ auth (extends accounts.py)                                      │
│   api/demo.py                ─ kept as-is (public anonymous demo tier — "free forgets")        │
│                                                                                                │
│  DOMAIN (reused, untouched)                         NEW GLUE                                   │
│   matching/pipeline.py  run_matching()               ingestion/jd_structure|jd_fields|         │
│   matching/{ranker,retrieval,rerank,evaluator}.py      jd_merge.py  (JD_AUTOFILL.md)           │
│   matching/{coaching,counterfactual,jd_audit}.py     inference/posting_schema.py               │
│   inference/adapter.py  get_adapter()  + redaction   stores/db.py (SQLite + migrations)        │
│   inference/adapters/claude_cli.py (extraction LLM)  stores/{postings,profiles,               │
│   ingestion/parser.py   extract_bytes_text()           applications,match_store}.py            │
│   audit/{metrics,proxy_leakage,defense_file,         workers/runner.py (DB-backed job queue,   │
│          compliance_pack}.py                            implements the SessionStore contract)  │
└───────────────┬──────────────────────────────┬───────────────────────────┬────────────────────┘
                │                              │                           │
     ┌──────────▼──────────┐        ┌──────────▼──────────┐     ┌──────────▼──────────────┐
     │ data/platform.db    │        │ data/audit.db       │     │ claude CLI subprocesses │
     │ (SQLite, WAL)       │        │ (SQLite, SEPARATE   │     │ (RM_INFERENCE_BACKEND=  │
     │ scoring plane:      │        │  FILE — audit plane)│     │  claude_cli; mock in CI)│
     │ users,orgs,postings,│        │ self_id only;       │     └─────────────────────────┘
     │ resumes,applications│        │ AuditStore-shaped;  │
     │ match_results, jobs │        │ no cross-file joins │
     └─────────────────────┘        └─────────────────────┘
```

The two-database split is boundary #2 (`stores/data_planes.py`) made physical: `ScoringStore` and
`AuditStore` become thin DAOs over two separate SQLite files, keeping `PROTECTED_KEYS` ingress
rejection and `labels_for()` as the only audit egress. **No `ATTACH DATABASE`, ever.** CI asserts
platform.db contains no `PROTECTED_KEYS` column. (SQLite has no credential roles, so separation
is file + CI + convention; the Postgres port must adopt separate credentials per plane — that is
the recorded upgrade condition, see risk register.)

---

## Roles & auth

Extend `api/accounts.py` — the PBKDF2 + hashed-token + HttpOnly-cookie core is kept verbatim
(`register/login/_issue_token/user_for_token`, accounts.py:96–161).

- **Schema:** add `role TEXT CHECK(role IN ('student','employer','coordinator','admin'))` and
  `org_id` to `users`; add `orgs` and `employer_school_links`. Coordinators are seeded by an
  admin CLI (`scripts/create_user.py`); employers self-register but stay unlinked until a
  coordinator approves the org↔school link (Handshake's trust model; doubles as spam control).
- **Authorization:** one new dependency, `require_role(*roles)` in `api/auth.py` (403 otherwise,
  attaches user). Ownership checks keep the existing `WHERE user_id=?` / `WHERE org_id=?`
  pattern from the projects routes.
- **The shared-password admin gate is demoted, not deleted:** it guards only `/api/ops/*` and
  the legacy synthetic dashboard. Everything else authenticates per-user via `rm_session`.
- **SSO deferred, deliberately:** phases 1–2 run email+password with a pilot cohort under
  explicit written consent; Phase 3 adds OIDC (York Azure AD; "Sign in with LinkedIn" for
  employers — boundary #1 allows OIDC, never scraping). Only user-row creation changes.
  **Gate:** confirm with York that this sequencing survives their PIA before Phase 2 starts.

---

## Data model

Migrations: `stores/db.py` with a `schema_version` table + numbered
`stores/migrations/00N_*.sql` applied at startup (~50 lines; replaces the
`executescript CREATE IF NOT EXISTS` non-story in accounts.py). `accounts.db` folds in as
migration 001. Every core table carries `school_id` (York = the only row for now).

```
schools          id, name                                            -- tenancy graft: 1 row today
orgs             id, name, website, created_at
employer_school_links  org_id, school_id, status (pending|approved|revoked),
                 reviewed_by, reviewed_at                            -- approval is a LINK, not a flag
users            id, school_id, email, pw_hash, salt, role, org_id NULL, created_at
tokens           (existing, unchanged)

postings         id, school_id, org_id, created_by, status           -- draft | pending_review |
                 title, description, location, work_mode,            --   live | closed | rejected
                 employment_type, pay_min, pay_max, apply_deadline,
                 min_education, min_years,
                 extraction_json,        -- per-field {value, source_span, method, confidence}
                 ai_disclosure_inserted INT NOT NULL DEFAULT 1,      -- WFWA block (graft #8)
                 reviewed_by, reviewed_at, created_at, updated_at
posting_skills   posting_id, skill_id, bucket (required|preferred|must_have), source
posting_events   id, posting_id, actor_user_id, from_status, to_status, note, at  -- append-only

student_profiles user_id PK, school_id, program, grad_year, work_auth_simple, visibility, updated_at
resumes          id, user_id, filename, content_type, file_blob BLOB, extracted_text,
                 redacted_text, uploaded_at, deleted_at NULL         -- hard-DELETE honored
consents         id, user_id, purpose (resume_storage|profile_matching|self_id_audit|contact),
                 version, granted_at, revoked_at NULL                -- append-only; pool filter
                                                                     -- applies BEFORE retrieve()

applications     id, posting_id, student_id, resume_id, status       -- applied | shortlisted |
                 human_review_requested INT DEFAULT 0,               --   advanced | rejected | hired
                 note, created_at, updated_at

match_results    posting_id, student_id, fit_score REAL, grade TEXT,
                 score_kind TEXT NOT NULL
                   CHECK(score_kind='fit_readiness_not_hire_probability'),   -- boundary #4 in schema
                 result_json,            -- serialized ScoreResult (api/serialize.py)
                 engine_version, computed_at    PK(posting_id, student_id)

jobs             id, kind (extract_posting|match_posting|rematch_student),
                 owner_user_id, status (queued|running|done|error),
                 attempts INT, run_after, locked_by, dedupe_key UNIQUE,
                 progress_done, progress_total, payload_json, result_json, error,
                 created_at, started_at, finished_at

events           id, actor_user_id, action, entity, entity_id, at    -- append-only audit log

-- audit.db (separate file, AuditStore only):
self_id          candidate_ref, attr CHECK(attr IN AUDITABLE_ATTRIBUTES), value, at
defense_records  hash-chained defense-file rows, now including EXPOSURE EVENTS:
                 (viewer, posting, candidate refs, rank order, ts) on first shortlist view
```

Resume `file_blob`s live in SQLite for MVP — atomic delete honors the consent contract; move to
disk/S3 only when size hurts. **Backups are PII copies: the backup target must be encrypted and
retention-bounded from day one** (risk register #5).

---

## Absorbing the matching engine & demo flow

The engine needs **zero changes** — `run_matching` (matching/pipeline.py) and `evaluate_many`
(evaluator.py) are already stateless. What changes is who feeds them:

- **`workers/runner.py`:** a small pool of daemon threads (started in `create_app()` lifespan,
  like the current TTL sweeper) polling the `jobs` table. It implements exactly the
  `SessionStore.create_pending / update_progress / complete / fail` contract (demo.py:223–286).
  Claim semantics: `UPDATE jobs SET status='running', locked_by=? WHERE id=? AND
  status='queued'`. On startup, `running` jobs older than a threshold are re-queued (fixes the
  "202 placeholder dies on restart" hole); `attempts` + `run_after` give poison jobs bounded
  retry-with-backoff instead of a silently wedged worker.
- **`match_posting` job:** when a posting goes `live`, build a `JobSpec` from
  `postings`+`posting_skills` via `build_job_spec` (ingestion/job_posting.py), build
  `CandidateProfile`s from **visible, consent-granted** `resumes.redacted_text`, run
  retrieve → rerank → evaluate_many → score, upsert `match_results`.
- **Rematch is event-driven by default** (resume updated → `rematch_student`; posting
  edited/approved → `match_posting`), NOT a nightly full matrix — the judges flagged
  postings × students as the first throughput wall. Batch windows are the fallback, not the plan.
- **The redaction boundary survives unchanged:** `redacted_text` is produced at upload
  (parser.py); the `is_local=False` tripwire in `inference/adapter.py` is untouched (boundary #3).
- **The anonymous demo (`api/demo.py`) is kept as-is** as the public tier — ephemeral, RAM-only,
  TTL'd. Its "free forgets" posture becomes a deliberate contrast with logged-in storage
  ("accounts remember, with consent"). Per-IP quota stays for the demo; logged-in runs meter on
  `user_id`.

---

## API surface (new/changed; per-user auth except noted)

```
Auth        POST /api/account/register|login|logout   (existing, +role/org at register)
            GET  /api/account/me                      (returns role, org, school)

Postings    POST /api/postings/extract                employer|coordinator; 202+poll (flagship)
            GET  /api/postings/extract/{job_id}
            POST /api/postings                        create draft / submit for review
            GET  /api/postings?status=&org=           role-scoped (students see live only)
            GET/PATCH /api/postings/{id}              owner or coordinator
            POST /api/postings/{id}/close

Coordinator POST /api/coordinator/postings/{id}/approve|reject
            POST /api/coordinator/org-links/{org_id}/approve|revoke
            GET  /api/coordinator/queue
            GET  /api/coordinator/audit               selection/exposure metrics over REAL
            GET  /api/coordinator/compliance-pack.json  applications (replaces synthetic AppState)

Students    PUT  /api/students/me/profile
            POST /api/students/me/resume              multipart; parse+redact at ingest
            DELETE /api/students/me/resume            hard delete (ephemerality contract)
            GET/POST /api/students/me/consents        grant/revoke per purpose
            GET  /api/students/me/matches             closest_fit + coach per live posting
            POST /api/postings/{id}/apply             creates application

Matches     GET  /api/postings/{id}/shortlist         employer(own)|coordinator; ranked;
                                                      first view writes an EXPOSURE EVENT
            GET  /api/postings/{id}/shortlist.csv
            PATCH /api/applications/{id}              status transitions
            GET  /api/postings/{id}/defense-file.json

Jobs        GET  /api/jobs/{id}                       generic 202-poll endpoint (DB-backed)

Kept as-is  /api/demo/*  (anonymous tier), /api/verify, /api/health, /api/ops/* (admin gate)
```

---

## Storage & deployment

**SQLite (WAL), two files, on the existing Docker volume.** One school ≈ tens of thousands of
students, a few hundred live postings, one writer process — SQLite is the correct boring choice
and is already in the repo (`AccountStore`). All SQL goes through `stores/db.py`, parameterized,
no ORM: the Postgres port is mechanical and its trigger conditions are listed below.

**Deployment: unchanged pipeline, new host at pilot.** Same Dockerfile/compose/Caddy with
health-gated deploy. Dev/demo stays on the cohost VPS; the **York pilot with real students
requires jumping the DEPLOY.md legal gate** — dedicated VM, encrypted disk, FIPPA
notice/PIA/consent paperwork, `mem_limit ≥ 4 GB` for the claude_cli subprocess fan-out.

---

## Build order

**Phase 1 — Postings & JD-autofill (the flagship), ~2–3 weeks**
1. `stores/db.py` + migrations (incl. `school_id`, `consents`, `posting_events`, graft columns);
   fold accounts.db in; roles/orgs/links; `require_role`; demote admin gate. (3–4 d)
2. `workers/runner.py` DB-backed jobs (+attempts/backoff/requeue) + `GET /api/jobs/{id}`. (2 d)
3. JD-autofill pipeline per [`JD_AUTOFILL.md`](JD_AUTOFILL.md): `posting_schema.py` + pinned
   schema, deterministic passes, LLM pass, merge/confidence. (4–5 d)
4. Postings CRUD + coordinator queue + employer review form + WFWA disclosure at publish. (4–5 d)
   *Exit demo: employer pastes a real JD → pre-filled provenance-annotated form → coordinator
   approves → live posting.*

**Phase 2 — Students & the matching loop, ~2–3 weeks**
*(gate first: York FIPPA/PIA/SSO sequencing confirmed)*
1. Student profile + consent grants + resume upload (parse, redact, store) + hard delete. (3 d)
2. `match_posting`/`rematch_student` jobs wiring `run_matching`; `match_results` store;
   exposure-event write on first shortlist view. (3–4 d)
3. Browse/apply + applications pipeline + employer ranked shortlist + CSV. (4 d)
4. Student "roles for you" (`closest_fit` + `coach`). (2 d)
5. Email notifications (stdlib SMTP: application received, posting approved). (1–2 d)

**Phase 3 — Compliance & institution-readiness, ~3–4 weeks**
1. Wire `selection_audit`/`exposure_parity`/`proxy_leakage` to real applications; voluntary
   self-ID form → audit.db; min-cell-5 everywhere. (4 d)
2. Per-posting defense files + term compliance pack (reuse signed-envelope builders). (2 d)
3. OIDC SSO (York Azure AD; LinkedIn OIDC for employers). (4–5 d)
4. Pilot hosting move + FIPPA/PIA checklist + encryption at rest. (ops, elastic)
5. Postgres port **only if** the pilot shows writer contention — and when it happens, adopt
   C's separate-credentials-per-plane design, not two schemas in one DB. (3 d when needed)

UI estimates carry the judges' health warning: three role-specific vanilla-HTML UIs in 4–5 days
is optimistic by 2–3×. UI is where flagship perception lives — if it slips, cut student-side
polish, never the employer review form.

---

## Trade-offs accepted & risk register

**Accepted:** SQLite over Postgres (port isolated to `stores/db.py` + `workers/runner.py`).
In-process worker threads over Celery/Redis (jobs re-queue from the DB on restart; no broker to
operate). Vanilla HTML over a SPA (matches demo.html; maintainable by 1 dev). Email+password
before SSO (pilot-consent-gated). Blobs in SQLite. Single-York bet with `school_id` as the hedge.

**Breaks first, in order:**
1. **LLM extraction throughput** — 4 concurrent `claude` subprocesses ≈ a few hundred
   extractions/hour; a career-fair spike queues for hours. Knob first
   (`RM_CLAUDE_CLI_CONCURRENCY`), then a second worker box (forces #3).
2. **Rematch volume** — event-driven-only is the default for exactly this reason; batch windows
   if event volume itself spikes.
3. **Single process** — one crash = total outage; the moment the Postgres + external-queue port
   pays for itself.
4. **SQLite write contention** — only after #3 (one process serializes writers anyway).
5. **Vanilla-HTML coordinator UI** — beyond ~10 staff workflows it becomes the maintenance sink;
   rewriting it touches static pages only, not the API.

**Risk register (from the judge panel):**
1. *FIPPA/SSO sequencing* — may be a phase-0 gate, not phase-3; validate with York before
   Phase 2 or the student side stalls on paperwork mid-pilot.
2. *Poison jobs* — mitigated by `attempts`/`run_after`/startup-requeue; watch for hung
   `claude_cli` subprocesses specifically.
3. *Extraction throughput wall* (above) — the nightly-full-matrix design was rejected for this.
4. *UI optimism* — 2–3× buffer on the three role UIs.
5. *Backups are PII copies* — encrypted, retention-bounded backup target from day one.
6. *Second school arrives early* — `school_id` + `employer_school_links` grafts are the hedge;
   posting-visibility queries are the retrofit surface.
7. *Plane separation is convention+CI under SQLite* — re-verify the day a second process
   appears; Postgres port must move to per-plane credentials.
