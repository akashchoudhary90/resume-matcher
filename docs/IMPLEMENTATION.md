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

## Slice D — deterministic passes P0–P3

- [ ] D1 `ingestion/jd_structure.py` — sectionizer (EN+FR heading regexes, bullet runs,
      char offsets), kinds: header/about_company/responsibilities/qualifications_required/
      qualifications_preferred/pay_benefits/application/eeo_boilerplate/other.
- [ ] D2 `ingestion/jd_fields.py` — deterministic extractors returning ExtractedField:
      email/url/phone (reuse redaction.py patterns, capturing), pay (currency+range+period),
      deadline vs start date, employment_type, work_mode, min_education + min_years (scoped to
      qualifications sections), responsibilities/qualification bullet lines, per-section
      `normalize_skills`, title/employer heuristics. Promote `_skill_ids_from_names` here;
      `api/demo.py` re-exports it (test imports keep working).
- [ ] D3 P3 scan: reuse `scan_injection` + language heuristic + multi-role heuristic → flags.
- [ ] D4 Tests: `tests/test_jd_structure.py`, `tests/test_jd_fields.py` (incl. URL-junk and
      about-company-years regressions), fixture JDs under `tests/fixtures/jds/`.

## Slice E — LLM pass P4 + merge P5

- [ ] E1 `claude_cli.extract_posting(jd_text, title)` + `_POSTING_SYSTEM` (fence, no-authority,
      verbatim quote per field, multi_role_detected, language).
- [ ] E2 `ingestion/jd_merge.py` — `verify_span(text, quote)` (extracted/shared with ranker's
      logic), per-field merge table (agree→high, regex-exact→high, LLM+verified→medium,
      conflict/unverified→low), clamps (dates/pay/enums/URL/skill caps 4/12/10), skill
      resolution via `_skill_ids_from_names`, dual-detector skill agreement, adjacency dedup.
- [ ] E3 `ingestion/posting_extract.py` — orchestrator `extract_posting_draft(text|file bytes,
      filename, backend)` running P0→P5 with `_EXTRACT_CACHE`-style caching + fail-open
      (LLM down → P1+P2 draft flagged `llm_unavailable`).
- [ ] E4 Tests: `tests/test_jd_merge.py` (quote verification, merge matrix, clamps),
      `tests/test_posting_extract.py` (hermetic `_arm`-style: LLM ok / junk / down / injection
      fixture → flags fire, nothing red auto-accepts).

## Slice F — platform stores + API routes

- [ ] F1 `stores/platform.py` — PostingStore (create_draft from extraction, get, patch fields,
      submit→pending_review, approve/reject→live/rejected + posting_events rows, list scoped
      by role, skills CRUD), OrgStore (create, link to school, approve link).
- [ ] F2 `api/platform.py` (APIRouter, mounted when RM_PLATFORM_ENABLED=1):
      POST /api/postings/extract (text or multipart; rate-limited; enqueues extract_posting
      job → 202 {job_id, poll}), POST /api/postings (draft from reviewed payload),
      GET /api/postings (role-scoped), GET/PATCH /api/postings/{id},
      POST /api/postings/{id}/submit|close, GET /api/coordinator/queue,
      POST /api/coordinator/postings/{id}/approve|reject,
      POST /api/coordinator/org-links/{org_id}/approve, GET /api/skills (typeahead, promoted).
- [ ] F3 WFWA disclosure: `AI_DISCLOSURE` constant appended to description at approve-time
      (never employer-removable).
- [ ] F4 extract_posting job handler registered in the worker pool (runs
      extract_posting_draft, stores result as job result_json).
- [ ] F5 Tests: `tests/test_platform_api.py` — full lifecycle employer→extract→create→submit→
      coordinator approve→live(+disclosure), role denials (student can't post, employer can't
      approve), org-link gate blocks submit until approved.

## Slice G — the two UIs

- [ ] G1 `static/employer.html` — paste/upload JD → poll → two-pane review (source with span
      highlight on field focus; right-pane form with per-field method/confidence badges, green/
      amber/red policy rendering, skill chips + typeahead, publish gating per policy table) →
      create+submit. Vanilla fetch idioms from demo.html.
- [ ] G2 `static/coordinator.html` — queue list, open posting (same evidence view read-only),
      approve/reject with note, org-link approvals.
- [ ] G3 Pages routed (`GET /employer`, `GET /coordinator`) behind the platform flag; nav links.
- [ ] G4 Verified in browser (preview server, mock backend): full employer flow + coordinator
      approve; console clean. (Playwright E2E deferred — manual verify via Browser pane.)

## Slice H — hardening + ship

- [ ] H1 Corrections→eval loop: on create-after-review, diff draft vs submitted; append
      `data/eval/jd_extraction_corrections.jsonl` (strip contact payloads). 
- [ ] H2 `RM_PLATFORM_ENABLED=1` in `.claude/launch.json` env (local), README + DEPLOY.md note;
      decide prod flip separately (needs coordinator seed on VPS).
- [ ] H3 Full `pytest -q` + ruff clean; update this file's boxes; final commit + push.
- [ ] H4 (stretch) `audit_requirements` widget on coordinator view; vision fallback for
      scanned JD PDFs; French heading set. Not MVP-blocking.

## Env vars added

| Var | Default | Meaning |
|---|---|---|
| `RM_PLATFORM_ENABLED` | `0` | mount platform routes + start worker pool |
| `RM_PLATFORM_DB` | `data/platform.db` | platform + accounts SQLite file |
| `RM_PLATFORM_WORKERS` | `2` | worker threads |
| `RM_JOB_MAX_ATTEMPTS` | `3` | retry ceiling per job |

## Restart protocol

1. `git log --oneline -5` + read this file → find first unchecked box.
2. `pytest -q` to confirm the tree is green before continuing.
3. Continue the slice; keep commits slice-scoped; update boxes in the same commit.
