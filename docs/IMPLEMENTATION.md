# Phase-1 MVP implementation checklist — platform + JD-autofill

**This file is the restart mechanism.** Work proceeds in slices; every slice ends with green
tests and a commit that flips its checkbox here. If a session dies, a fresh session reads this
file, finds the first unchecked box, and continues. Specs: [`PLATFORM.md`](PLATFORM.md) +
[`JD_AUTOFILL.md`](JD_AUTOFILL.md). Boundaries: [`DESIGN.md`](DESIGN.md) (never violate).

**Definition of done (Phase-1 MVP):** an employer registers → pastes/uploads a JD →
`POST /api/postings/extract` (202) → pre-filled provenance-annotated draft → employer review form
→ submit → coordinator queue → approve → posting `live` with the WFWA AI-disclosure block.
All existing tests stay green. Everything behind `RM_PLATFORM_ENABLED` (default **off**) so
pushes stay safe to auto-deploy.

## Ground rules (apply to every slice)

- SQLite via `stores/db.py` only; parameterized SQL; no ORM; no `ATTACH`.
- New platform routes live under `/api/platform/*`-agnostic paths but are mounted only when
  `RM_PLATFORM_ENABLED=1` (config.py). Existing `/api/demo/*` and dashboard untouched.
- LLM never decides; deterministic code validates (quotes verified verbatim, clamps, caps).
- Run `pytest -q` before each commit; commit message names the slice; push (auto-deploy is safe
  because the flag defaults off).

## Slice A — foundations: DB, migrations, roles ✅

- [x] A1 `resume_matcher/stores/db.py` — `platform_db_path()` (env `RM_PLATFORM_DB`, default
      `data/platform.db`), `connect()`, `migrate(path)` with `schema_version` table, applies
      `resume_matcher/stores/migrations/NNN_*.sql` in order + python column-upgrade helper
      (`_ensure_columns`) for ALTERs on pre-existing tables.
- [x] A2 `stores/migrations/001_platform.sql` — schools (seed York), orgs,
      employer_school_links, users(+role/org_id/school_id), tokens, projects, postings,
      posting_skills, posting_events, consents, jobs, events, student_profiles, resumes,
      applications, match_results (score_kind NOT NULL CHECK). All with school_id.
- [x] A3 Legacy fold-in: on migrate, if users empty and legacy `RM_ACCOUNTS_DB`/accounts.db
      exists → copy users/tokens/projects rows.
- [x] A4 `api/accounts.py` — AccountStore defaults to the platform DB; `register(..., role,
      org_name)`; `user_for_token` returns role/org_id/school_id. Existing demo/account tests
      stay green (default role `student`; legacy DBs upgraded by migrate()).
- [x] A5 `api/auth.py` — `require_role(*roles)` FastAPI dependency (401 no user, 403 wrong
      role) reading the `rm_session` cookie via the shared AccountStore.
- [x] A6 `scripts/create_user.py` — seed coordinator/admin from CLI.
- [x] A7 Tests: `tests/test_platform_db.py` (fresh migrate, re-migrate idempotent, legacy
      fold-in, protected-column CI grep over platform schema), `tests/test_require_role.py`.

## Slice B — DB-backed job runner ✅

- [x] B1 `resume_matcher/workers/__init__.py` + `workers/runner.py` — `JobStore`
      (enqueue/claim/progress/complete/fail/requeue_stale; columns per PLATFORM.md incl.
      attempts, run_after, locked_by, dedupe_key), `WorkerPool` (daemon threads, handler
      registry by kind, backoff = min(2**attempts, 60)s, max attempts 3).
- [x] B2 Wire into `create_app()` lifespan when `RM_PLATFORM_ENABLED=1`: start pool, requeue
      stale `running` jobs on boot, stop on shutdown.
- [x] B3 Route `GET /api/jobs/{id}` (owner or coordinator) — generic 202-poll payload
      {status, progress, result?, error?}.
- [x] B4 Tests: `tests/test_job_runner.py` — enqueue→run→done, failure→retry→error after max,
      dedupe_key idempotency, stale requeue, poll route auth.

## Slice C — posting schema (the contract) ✅

- [x] C1 `resume_matcher/inference/posting_schema.py` — `ExtractedField` envelope
      {value, source_span, source_page, method, confidence, status}, `JobPosting` (fields per
      JD_AUTOFILL.md §1 incl. work_authorization display-only), `PostingExtraction` (LLM wire
      shape: values + verbatim quotes, no IDs/confidences), `to_job_spec()` projection with
      PROTECTED_KEYS tripwire.
- [x] C2 `inference/job_posting.schema.json` generated + CI-pinned (test like
      test_prompt/schema pin), `posting_extraction.schema.json` pinned too.
- [x] C3 Tests: `tests/test_posting_schema.py` — round-trip, projection drops
      work_authorization/contact, tripwire fires on a protected key, schema files match models.

## Slice D — deterministic passes P0–P3 ✅

- [x] D1 `ingestion/jd_structure.py` — sectionizer (EN+FR heading regexes, bullet runs,
      char offsets), kinds: header/about_company/responsibilities/qualifications_required/
      qualifications_preferred/pay_benefits/application/eeo_boilerplate/other.
- [x] D2 `ingestion/jd_fields.py` — deterministic extractors returning ExtractedField:
      email/url/phone (reuse redaction.py patterns, capturing), pay (currency+range+period),
      deadline vs start date, employment_type, work_mode, min_education + min_years (scoped to
      qualifications sections), responsibilities/qualification bullet lines, per-section
      `normalize_skills`, title/employer heuristics. Promote `_skill_ids_from_names` here;
      `api/demo.py` re-exports it (test imports keep working).
- [x] D3 P3 scan: reuse `scan_injection` + language heuristic + multi-role heuristic → flags.
- [x] D4 Tests: `tests/test_jd_structure.py`, `tests/test_jd_fields.py` (incl. URL-junk and
      about-company-years regressions), fixture JDs under `tests/fixtures/jds/`.

## Slice E — LLM pass P4 + merge P5 ✅

- [x] E1 `claude_cli.extract_posting(jd_text, title)` + `_POSTING_SYSTEM` (fence, no-authority,
      verbatim quote per field, multi_role_detected, language).
- [x] E2 `ingestion/jd_merge.py` — `verify_span(text, quote)` (extracted/shared with ranker's
      logic), per-field merge table (agree→high, regex-exact→high, LLM+verified→medium,
      conflict/unverified→low), clamps (dates/pay/enums/URL/skill caps 4/12/10), skill
      resolution via `_skill_ids_from_names`, dual-detector skill agreement, adjacency dedup.
- [x] E3 `ingestion/posting_extract.py` — orchestrator `extract_posting_draft(text|file bytes,
      filename, backend)` running P0→P5 with `_EXTRACT_CACHE`-style caching + fail-open
      (LLM down → P1+P2 draft flagged `llm_unavailable`).
- [x] E4 Tests: `tests/test_jd_merge.py` (quote verification, merge matrix, clamps),
      `tests/test_posting_extract.py` (hermetic `_arm`-style: LLM ok / junk / down / injection
      fixture → flags fire, nothing red auto-accepts).

## Slice F — platform stores + API routes ✅

- [x] F1 `stores/platform.py` — PostingStore (create_draft from extraction, get, patch fields,
      submit→pending_review, approve/reject→live/rejected + posting_events rows, list scoped
      by role, skills CRUD), OrgStore (create, link to school, approve link).
- [x] F2 `api/platform.py` (APIRouter, mounted when RM_PLATFORM_ENABLED=1):
      POST /api/postings/extract (text or multipart; rate-limited; enqueues extract_posting
      job → 202 {job_id, poll}), POST /api/postings (draft from reviewed payload),
      GET /api/postings (role-scoped), GET/PATCH /api/postings/{id},
      POST /api/postings/{id}/submit|close, GET /api/coordinator/queue,
      POST /api/coordinator/postings/{id}/approve|reject,
      POST /api/coordinator/org-links/{org_id}/approve, GET /api/skills (typeahead, promoted).
- [x] F3 WFWA disclosure: `AI_DISCLOSURE` constant appended to description at approve-time
      (never employer-removable).
- [x] F4 extract_posting job handler registered in the worker pool (runs
      extract_posting_draft, stores result as job result_json).
- [x] F5 Tests: `tests/test_platform_api.py` — full lifecycle employer→extract→create→submit→
      coordinator approve→live(+disclosure), role denials (student can't post, employer can't
      approve), org-link gate blocks submit until approved.

## Slice G — the two UIs ✅

- [x] G1 `static/employer.html` — paste/upload JD → poll → two-pane review (source with span
      highlight on field focus; right-pane form with per-field method/confidence badges, green/
      amber/red policy rendering, skill chips + typeahead, publish gating per policy table) →
      create+submit. Vanilla fetch idioms from demo.html.
- [x] G2 `static/coordinator.html` — queue list, open posting (same evidence view read-only),
      approve/reject with note, org-link approvals.
- [x] G3 Pages routed (`GET /employer`, `GET /coordinator`) behind the platform flag; nav links.
- [x] G4 Verified in browser (preview server, mock backend): full employer flow + coordinator
      approve; console clean. (Playwright E2E deferred — manual verify via Browser pane.)

## Slice H — hardening + ship ✅ (H4 stretch items deliberately deferred)

- [x] H1 Corrections→eval loop: on create-after-review, diff draft vs submitted; append
      `data/eval/jd_extraction_corrections.jsonl` (strip contact payloads). 
- [x] H2 `RM_PLATFORM_ENABLED=1` in `.claude/launch.json` env (local), README + DEPLOY.md note;
      decide prod flip separately (needs coordinator seed on VPS).
- [x] H3 Full `pytest -q` + ruff clean; update this file's boxes; final commit + push.
- [ ] H4 (stretch) `audit_requirements` widget on coordinator view; vision fallback for
      scanned JD PDFs; French heading set. Not MVP-blocking.

---

# Phase 2 — to ~80% of Handshake functionality (goal set 2026-07-12)

Target inventory (Handshake-parity checklist): postings ✅, employer trust gate ✅, coordinator
approvals ✅, JD-autofill ✅ (our differentiator) — plus the slices below. The deliberate missing
~20%: events/career fairs, messaging, interview scheduling, mobile.

**York data-isolation stance (recorded):** assume York will NOT allow student PII to a third-party
LLM. The engine already gates this (redaction chokepoint + `is_local` tripwire + swappable
adapter); Slice N makes the JD-autofill LLM pass backend-agnostic too, so a fully ISOLATED
deployment is `RM_INFERENCE_BACKEND=ollama` + `RM_PLATFORM_EXTRACT_BACKEND=ollama` (nothing leaves
the box) and Claude stays a dev/demo convenience. JDs are employer marketing text (not student
PII) — lower sensitivity — but the same switch covers them.

## Slice I — students: profile, consents, resume ✅

- [x] I1 `stores/students.py` — StudentStore: profile upsert/get; consents grant/revoke/active
      (append-only rows); resume save (blob + extracted + REDACTED text, one active resume per
      student, replace = hard-delete old row) + hard delete; `matchable_students()` = visible
      profiles with active resume AND active `profile_matching` consent (pool filter BEFORE
      retrieval).
- [x] I2 Routes: GET/PUT /api/students/me/profile, GET/POST /api/students/me/consents (grant/
      revoke by purpose), POST /api/students/me/resume (multipart; parse_resume_bytes reuse;
      requires `resume_storage` consent), DELETE /api/students/me/resume, GET meta.
- [x] I3 Tests: consent gate blocks upload; hard delete removes blob+text; matchable pool
      respects visibility+consent+resume; redacted_text has no direct identifiers.

## Slice J — browse + apply + application pipeline ✅

- [x] J1 ApplicationStore (in stores/students.py): apply (live posting + own resume), list mine
      (student), list for posting (employer own org / coordinator), status transitions
      applied→shortlisted→advanced→rejected|hired, human_review_requested flag.
- [x] J2 Routes: POST /api/postings/{id}/apply, GET /api/students/me/applications,
      GET /api/postings/{id}/applications, PATCH /api/applications/{id} (role-gated),
      POST /api/applications/{id}/request-human-review (student).
- [x] J3 Tests: student applies once (dupe 409), employer sees own org's applicants only,
      transitions validated, non-live posting rejects applications.

## Slice K — the matching loop (the engine goes live) ✅

- [x] K1 `stores/matches.py` MatchStore — upsert/get match_results (score_kind CHECK),
      shortlist(posting), roles_for(student) over live postings.
- [x] K2 Job handlers: `match_posting` (enqueued at approve; scores matchable pool vs posting via
      build_job_spec + CandidateProfile-from-redacted_text + get_adapter() + evaluator — engine
      untouched), `rematch_student` (enqueued at resume upload; scores vs live postings).
      Event-driven only.
- [x] K3 Routes: GET /api/postings/{id}/shortlist (employer own/coordinator; ranked, full
      breakdown from result_json; joins applications) — first view per (viewer, posting) writes an
      EXPOSURE EVENT to the append-only events table; GET /api/students/me/matches (roles for
      you: fit + gaps per live posting).
- [x] K4 Tests: approve → match job runs (mock adapter) → shortlist ranked; resume upload →
      rematch; consent revoke removes student from next run; exposure event written once per
      viewer.

## Slice L — student coaching surface (thin) ✅

- [x] L1 Student match detail includes the score explanation + gaps (already in result_json).
- [x] L2 Tests: gaps/explanation present for a scored pair.

## Slice M — email notifications (stdlib, no-op unless configured) ✅

- [x] M1 `resume_matcher/notify.py` — send via smtplib when RM_SMTP_HOST set, else log+skip;
      fire on: org link approved, posting approved/rejected, application received.
- [x] M2 Tests: monkeypatched transport captures sends; unset config = silent no-op.

## Slice N — isolated LLM extraction (the York answer) ✅

- [x] N1 Generalize `posting_extract._llm_posting_extraction` to backends: claude_cli (as now),
      ollama, openai_compat — via adapter-level `extract_posting` using the pinned
      posting_extraction schema as a format constraint (same pattern as MatchExtraction).
- [x] N2 Boundary #3 in code: a non-local adapter (`is_local=False`) only ever sees a
      `redact_text`-ed JD copy (contacts already captured deterministically in P2), gated by
      `assert_redacted`.
- [x] N3 README "Isolated deployment (York mode)" section.
- [x] N4 Tests: hermetic adapter fake; non-local adapter receives redacted JD.

## Slice P — student UI + shortlist UI ✅

- [x] P1 `static/student.html` — profile+consents, resume upload/delete, browse live postings,
      apply, my applications, "roles for you" with fit + why + gaps.
- [x] P2 employer.html + coordinator.html: ranked shortlist view with expandable why-this-score.
- [x] P3 Browser-verified end to end with mock engine; console clean.

## Slice Q — ship the 80% ✅

- [x] Q1 Full pytest + ruff; boxes flipped; README student-flow update; commit + push.
- [x] Q2 Handshake-parity statement written into this file (what's in the 80%; the missing 20% =
      events/fairs, messaging, interviews, mobile).

## Env vars added

| Var | Default | Meaning |
|---|---|---|
| `RM_PLATFORM_ENABLED` | `0` | mount platform routes + start worker pool |
| `RM_PLATFORM_DB` | `data/platform.db` | platform + accounts SQLite file |
| `RM_PLATFORM_WORKERS` | `2` | worker threads |
| `RM_JOB_MAX_ATTEMPTS` | `3` | retry ceiling per job |
| `RM_PLATFORM_EXTRACT_BACKEND` | `claude_cli` | JD-autofill LLM pass backend (`ollama` = isolated) |
| `RM_PLATFORM_EXTRACT_PER_MIN` | `6` | per-user extract rate limit |
| `RM_SMTP_HOST/PORT/FROM` | (unset) | email notifications; unset = silent no-op |

## Restart protocol

1. `git log --oneline -5` + read this file → find first unchecked box.
2. `pytest -q` to confirm the tree is green before continuing.
3. Continue the slice; keep commits slice-scoped; update boxes in the same commit.

## Handshake-parity statement (Q2, 2026-07-12)

**In the ~80% (built, tested, browser-verified):** employer self-service accounts + org trust
gate; JD-autofill posting creation (the differentiator Handshake lacks); coordinator posting
approval workflow + append-only audit trail; student accounts, profiles, consent lifecycle,
resume upload with redaction-at-ingest and hard delete; job browse + apply + application status
pipeline + human-review-requested; evidence-quoted match rankings BOTH directions (employer
shortlists ranked by fit with verbatim-quote breakdowns + exposure-event logging; student
"roles for you" with why-this-score and gaps) — Handshake has none of this matching depth;
email notifications; isolated on-box LLM mode; bias-audit machinery (pre-existing) ready to
wire to real applications.

**The deliberate missing ~20%:** career-fair/event management, employer↔student messaging,
interview scheduling, mobile apps, multi-school marketplace network effects (schema is ready:
school_id + employer_school_links), OFCCP/EEO-style reporting exports.

---

# Phase 3 — the remaining 20% (goal set 2026-07-12: "don't stop till nothing is left")

## Slice R — events & career fairs ✅
- [x] R1 Migration 002: events(id, school_id, kind fair|info_session|workshop, title, description,
      location, starts_at, ends_at, created_by, status draft|published|cancelled),
      event_registrations(event_id, user_id, role, status registered|cancelled, created_at,
      UNIQUE(event_id,user_id)).
- [x] R2 stores/events.py EventStore: coordinator create/publish/cancel; list (students+employers
      see published, coordinators all); register/unregister (employer books a booth, student
      RSVPs); attendee list for coordinators/owning employer.
- [x] R3 Routes: POST /api/events (coordinator), PATCH /api/events/{id} (publish/cancel),
      GET /api/events, POST /api/events/{id}/register|unregister, GET /api/events/{id}/attendees.
- [x] R4 Tests: lifecycle, role gates, dupe registration 409, attendee visibility.

## Slice S — messaging (application threads) ✅
- [x] S1 Migration 002: messages(id, application_id, sender_user_id, body, sent_at, read_at).
- [x] S2 stores/messages.py: send (only the applicant, the posting org's employers, or a
      coordinator; only while the application exists), thread list, mark-read, unread counts.
- [x] S3 Routes: GET/POST /api/applications/{id}/messages, GET /api/messages/unread-count.
- [x] S4 Tests: both parties + coordinator can read/write; a rival employer cannot; unread flow.

## Slice T — interview scheduling ✅
- [x] T1 Migration 002: interview_slots(id, application_id, proposed_by, starts_at, ends_at,
      status proposed|accepted|declined|cancelled, created_at).
- [x] T2 stores/interviews.py: employer proposes 1..N slots; student accepts ONE (its siblings
      auto-decline); either side cancels; upcoming list per user.
- [x] T3 Routes: POST /api/applications/{id}/interview-slots (employer),
      GET /api/applications/{id}/interview-slots, POST /api/interview-slots/{id}/accept (student)
      |cancel, GET /api/students/me/interviews + employer equivalent.
- [x] T4 Tests: accept auto-declines siblings; only the applicant accepts; cancel rules.

## Slice U — mobile-ready (responsive + PWA) ✅
- [x] U1 manifest.webmanifest + theme-color + icons (inline SVG data URI), route it; link from all
      four platform pages.
- [x] U2 Responsive CSS for employer/coordinator/student pages (two-pane stacks under 900px,
      tables scroll in a wrapper, touch-sized buttons).
- [x] U3 Verify at 375px (browser resize): no horizontal scroll on the three pages.

## Slice V — multi-school marketplace (schema is ready; wire it through) ✅
- [x] V1 Schools API: GET /api/schools (public list for the register form),
      POST /api/schools (admin only). Registration accepts school_id (default York).
- [x] V2 Scope by school everywhere school_id=1 was implied: postings list/queues/match pool/
      events take school_id from the signed-in user; employer_school_links approval is per
      school; an employer may request a link to another school
      (POST /api/orgs/me/school-links {school_id}).
- [x] V3 Tests: two schools — a student at school B never sees school A postings; coordinator
      queues are per-school; employer posts to each school only after that school's link is
      approved.

## Slice W — EEO / funnel reports + self-ID (audit plane made persistent) ✅
- [x] W1 stores/audit_store.py: SEPARATE SQLite file data/audit.db (RM_AUDIT_DB) — self_id rows
      keyed by an opaque candidate ref; no connection ever opens both DBs (boundary #2 physical).
- [x] W2 Student voluntary self-ID route (consent purpose self_id_audit required) writing ONLY to
      the audit DB; delete-my-self-ID route.
- [x] W3 Coordinator funnel report: per-posting applications by status + shortlist exposure
      counts + selection rates; aggregate self-ID breakdown with MIN-CELL-5 suppression;
      GET /api/coordinator/reports/funnel.json + .csv.
- [x] W4 Tests: self-ID lands only in audit.db (platform.db has no such column — existing CI
      test), min-cell suppression, funnel counts correct, role gates.

## Slice X — UI wiring + ship ✅
- [x] X1 student.html: events card (RSVP), interviews card (accept slot), messages on my
      applications. employer.html: events card (book booth), applicant messaging + propose
      slots from the shortlist/applicants view. coordinator.html: events CRUD card + funnel
      report link + self-ID aggregate view.
- [x] X2 Browser-verify: event RSVP, a message round-trip, slot accept, 375px pass; console clean.
- [x] X3 Full pytest + ruff; flip boxes; update the parity statement (nothing deliberately
      missing except native mobile apps — the web app is installable/responsive); README update;
      commit + push; memory update.

## Handshake-parity statement — FINAL (Phase 3 complete, 2026-07-12)

Everything on the "missing 20%" list is now built, tested, and browser-verified:
career-fair/events (coordinator CRUD -> publish -> student RSVP / employer booths, attendee
lists), application-thread messaging (no cold outreach by design; unread counts; verified
round-trip in the UI), interview scheduling (propose N slots -> student accepts one, siblings
auto-decline), mobile-ready web app (PWA manifest, responsive at 375px with zero horizontal
scroll on all three pages), multi-school marketplace (hard school isolation for postings/queues/
match pools/events; per-school employer approval links; public schools API), and EEO/funnel
reporting (per-posting selection funnel + CSV export; voluntary self-ID in a PHYSICALLY separate
audit.db with aligned-ref egress and min-cell-5 suppression).

Remaining known gaps vs. Handshake (not platform features, deliberately out of scope): native
iOS/Android apps (the responsive PWA is installable instead) and Handshake's cross-school network
scale itself — which is a go-to-market fact, not software.

---

# Phase 4 — relationship graph & warm-intro engine (goal 2026-07-12: "implement remaining items")

Full spec + boundary compliance + adversarial-fix table: [`RELATIONSHIPS.md`](RELATIONSHIPS.md).
Consent-first warm intros: student → mutual → hiring manager over CONSENTED edges, double-opt-in.
The LinkedIn path is self-upload only, RAM-intersected, zero non-member residue. All behind
`RM_PLATFORM_ENABLED`. KMS pepper is a documented prod interface; dev uses `RM_GRAPH_PEPPER`
(RM_ENV=dev). PIA / legal opinion / data-residency are recorded launch gates (Slice AK), not code.

## Slice Y — migration 003 ✅ + unified consent rebuild + tenant hardening
- [x] Y1 `stores/migrations/003_phase4.sql` (RELATIONSHIPS.md §3): rebuild consents once with 4 new
      purposes (contacts_upload, graph_discoverable, warm_intro, network_analytics); member_graph_identity,
      graph_edges (ONE canonical edge table), graph_suppressions, employer_contacts, posting_contacts,
      vouches, intro_requests, intro_events, broker_blocks — all school_id NOT NULL (no default).
- [x] Y2 `students.py` CONSENT_PURPOSES += the 4 purposes.
- [x] Y3 Tests: protected-column CI still green, 003 idempotent (rows preserved on existing DB),
      purposes⇔CHECK match, school_id NOT NULL enforced.

## Slice Z — granular consent ✅ API + data-subject-request (erasure + non-member repudiation)
- [x] Z1 consent grant/revoke routes per purpose; DELETE /api/network (member erasure, hard delete +
      tombstone); POST /api/graph/repudiate (non-member DSR → graph_suppressions).
- [x] Z2 Tests: revoke hides member everywhere; erasure is hard delete; repudiation tombstones.

## Slice AA — tokenizer ✅ (KMS interface + dev pepper, per-school, versioned, fail-closed)
- [x] AA1 `stores/graph_tokens.py`: canonical_identity + identity_token(school_id,...) → (token, key_version);
      KMS-MAC interface, dev env-var pepper only when RM_ENV=dev, fail-closed without key.
- [x] AA2 Tests: canonicalization determinism, per-school divergence, key-version, fail-closed.

## Slice AB — contacts importer ✅ (RAM-only PSI-lite; zero non-member residue)
- [x] AB1 NetworkStore.import_csv + POST /api/network/import (202+poll); consent-gated; size/batch caps;
      RAM intersection vs member_graph_identity; discard non-matches before commit; no per-contact counts.
- [x] AB2 Tests: discards non-members (0 residue), no count egress, size/batch guards, fail-closed.

## Slice AC — edge builder ✅ + resolve/backfill (revocation-durable)
- [x] AC1 RelationshipStore.upsert_edge/build_native_edges/resolve_import/backfill; build_edges +
      resolve_network job handlers; native folding; never un-revoke; skip suppressed; post-consent only.
- [x] AC2 Tests: idempotent, never-unrevoke, skips suppressed, backfill ignores pre-consent, default pending.

## Slice AD — pathfinder ✅ (consent-gated BFS + verified-vouch ranking)
- [x] AD1 `stores/intros.py`: EDGE_STRENGTH, edge_score, rank_path, find_paths (BFS depth 3, top-5,
      product×recency), shared _SHAREABLE predicate; GET /api/intros/available/{posting} = bare boolean,
      gated behind an application, school-scoped, rate-limited.
- [x] AD2 Tests: ranking unit tests; respects shareable+both-consent; available is boolean-behind-application;
      no audit import.

## Slice AE — double-opt-in intro flow ✅ (authz-hardened)
- [x] AE1 intro lifecycle + routes; _intro_access READ-ONLY; accept/decline explicit broker-only 403;
      IDOR check (application.student_id==requester); broker pending cap + block; note redacted+escaped;
      intro_events status-only.
- [x] AE2 Tests: student can't accept own request, IDOR blocked, broker cap+block, decline is silent.

## Slice AF — vouches ✅ as structured job-related evidence
- [x] AF1 create/verify/contest vouch + routes; redact evidence at ingest; only verified tiers project a
      verified_vouch edge (self→low weight); subject view+contest; per-voucher rate limit; exposure logged.
- [x] AF2 Tests: self-vouch low weight, ingest redacts PII, subject view/contest, rate limit, contested not traversed.

## Slice AG — employer evidence card ✅
- [x] AG1 GET /api/intros/for-application/{id}; quoted attributable evidence, claim_kind, never in match_results.
- [x] AG2 Tests: card not in match_results, output escaped.

## Slice AH — fairness audit (aggregate-only, MIN_CELL=5)
- [ ] AH1 audit/metrics.access_disparity; GET /api/coordinator/reports/intro-equity (JSON+CSV); access +
      conversion funnels; two independent AuditDB.aggregate calls, never joined per-person.
- [ ] AH2 Tests: pass/fail/suppressed-numerator/single-group; two connections never joined.

## Slice AI — active mitigation as governed positive-action program
- [ ] AI1 NETWORK_FEATURE_KEYS guard in scoring plane; network_poverty (structural, never self-ID) trigger;
      alumni-bridge matcher + coordinator-initiated intro (double-opt-in); coverage report w/ shut-off.
- [ ] AI2 Tests: network features rejected by scoring plane; intros not in match_results; no audit import.

## Slice AJ — UI wiring
- [ ] AJ1 student.html (granular consents + attestation, contacts upload, discovery opt-in, delete-my-network,
      vouches-about-me, request-warm-intro behind application, intro inbox); coordinator.html (intro-equity +
      mitigation coverage); employer evidence card; broker inbox. All output escaped.
- [ ] AJ2 Browser-verify the full flow; console clean; 375px pass.

## Slice AK — retention/erasure job + legal launch gates
- [ ] AK1 graph_retention job (purge expired edges/intros; erasure cascade on account deletion; tombstones);
      record PIA + legal-opinion + data-residency + pepper-leak-runbook as documented launch gates.
- [ ] AK2 Tests: retention purges expired, erasure cascade leaves no PII, intro_events PII-free.
