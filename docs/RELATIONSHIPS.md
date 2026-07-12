# Relationship graph & warm-intro engine (Phase 4)

**Status: approved, adversarially reviewed (2026-07-12).** This spec was produced by a design panel
(4 component designs) then hardened by an adversarial privacy-lawyer + security-engineer pass; every
CRITICAL/HIGH finding is folded in as a build gate below. It is the durable design for the
consent-first relationship graph that replaces resume-era signal with verified relationships and
warm intros. Boundaries: [`DESIGN.md`](DESIGN.md) (never violate).

**The load-bearing decision:** the ONLY LinkedIn path is a member self-uploading their OWN export,
intersected in RAM against consenting on-platform members — non-members leave ZERO server-side
residue. No scraping, no fake accounts, no stored contact lists. See §0 and §6.

---

# Phase 4 — Consented Relationship Graph & Warm-Intro Pathfinder
## Unified, build-ready specification (tech-lead merge of 4 components + adversarial fixes)

Ships behind the existing `RM_PLATFORM_ENABLED` flag. Numbered migration is `003_phase4.sql` (Phase-3 shipped `002_phase3.sql`). Slices continue the Phase 1–3 lettering at **Y**.

---

## 0. Architecture decisions — how the four components were reconciled

The four component drafts contradicted each other in five places. Both reviewers flagged the contradictions as blockers. The merged design resolves each **one way**, and that resolution is a hard constraint on every slice below.

| Contradiction across drafts | Resolution (binding) |
|---|---|
| **Three edge tables** (`edges` / `graph_edges` / `relationship_edges`) with different columns and consent semantics | **ONE canonical table `graph_edges`.** Both endpoints are always registered `users.id` (members). There is no polymorphic user/external endpoint in the traversable graph. |
| **Three consent mechanisms** (`graph_share_consent` table + per-edge state; a `consents` rebuild for `network_sharing`; a second `consents` rebuild for `network_intro`) — and two of them rebuild `consents` in the *same* migration with conflicting CHECKs | **ONE rebuild of `consents` in 003**, adding all four granular purposes at once. `consents` is the single audited source of truth. Per-edge `consent_state` survives only as the owner's per-edge share toggle, and it **composes with** (never replaces) the global purposes. |
| **Imported contacts**: Component 2 says tokens-only, never a name; Component 1 stores raw `display_name`/`company` in `external_persons` and renders them | **Imported contacts are tokens-only and are NEVER materialized as a named row.** `external_persons` is deleted from the design. A member's LinkedIn contact only ever becomes an edge if it resolves to a consenting on-platform member; otherwise it leaves **zero server-side residue**. |
| **Non-member token storage**: Component 2 persists a token per non-member contact (`network_contacts`) | **We do not persist tokens for non-members.** Resolution is a RAM-only intersection against the consenting-member identity set at import time (PSI-lite); non-matching tokens are discarded before commit. Full PSI is the documented upgrade path. |
| **Pathfinder consent gate**: Component 3's traversal query filters only the far endpoint's purpose consent, ignoring the per-edge `consent_state='shareable'` gate that Components 1–2 built | **One shared traversal helper** enforces, in a single SQL predicate reused by every read path: `consent_state='shareable' AND revoked_at IS NULL AND BOTH endpoints hold active `graph_discoverable` consent`. |

Kept intact from the drafts (the strongest parts, praised by both reviewers): the boundary-#2 firewall (graph never enters `match_results`), the aggregate-only fairness egress with `MIN_CELL=5`, the double-opt-in reveal discipline, HMAC-over-plain-hash, and the honest non-guarantees section.

---

## 1. Boundary compliance — the four LOCKED boundaries

**Boundary #1 — no LinkedIn scraping / no fake accounts; only a member's own export; portable & deletable.**
- The only ingest path is a member self-uploading their **own** `Connections.csv` (Slice AB). The raw file is parsed in RAM and **never persisted** (no blob, no re-download endpoint — which also removes the incentive to upload someone else's file).
- No token is stored for any non-member (Decision 4). A contact only becomes durable state if it resolves to a consenting member.
- Every artifact a member creates is hard-deletable and self-contained (Slice Z erasure), and non-members get a repudiation/tombstone path (Slice Z) even though they have no account.

**Boundary #2 — protected attributes/proxies never enter scoring; the graph must not become an inference backdoor.**
- `graph_edges`, `vouches`, `intro_requests` live in the scoring-plane DB but are **never read by the matcher.** `matchable_students` (`students.py:142-152`) and `_match_posting_job` (`platform.py:548-560`) select only `redacted_text`/skills; no slice adds a graph join to any scoring query.
- No `003` column name collides with `PROTECTED_KEYS` (`data_planes.py:17-24`); no column is literally `name` (we never store contact names at all). CI grep in `tests/test_platform_db.py` passes with no allowlist edit.
- **Extended per Reviewer 1:** the no-proxy guarantee now covers **all opportunity-allocation surfaces, not just `match_results`.** Graph degree (`network_poverty`) drives mitigation (Slice AI), which is governed as an explicit, reviewed positive-action program with monitoring and a shut-off — never a silent proxy. A new `NETWORK_FEATURE_KEYS` guard (Slice AI) rejects `{degree, reachability, intro_count, connector_count, components}` from any scoring feature dict.
- Fairness is verified by two independent aggregate counts joined only on the opaque `candidate_ref` in the physically separate `audit.db` (Slice AH); there is no per-person join and no code path from `AuditDB` back into the pathfinder or mitigation.

**Boundary #3 — PII stays local by default; redaction chokepoint before any non-local LLM.**
- Vouch free-text is scanned for PII/sensitive attributes and redacted **at ingest, before persistence** (Slice AF) — not merely before an LLM. The pre-LLM redaction chokepoint remains, but persistence now happens on the already-redacted text.
- No slice sends graph data, tokens, or vouch text to a non-local LLM.

**Boundary #4 — no fabricated scores; honest fit/readiness; deterministic, evidence-quoted.**
- `graph_edges.weight` and `rank_path` scores are deterministic traversal-ranking conveniences, explicitly **not** hire-probabilities, and never written to `match_results`.
- A vouch carries `claim_kind='job_related_evidence_not_hire_recommendation'`, mirroring the CHECK-locked `score_kind='fit_readiness_not_hire_probability'` (`001:167-168`). It is displayed as quoted, attributable evidence, never blended into `fit_score`.
- The importer's honesty section (Slice AB §"non-guarantees") states plainly that per-school KMS-MAC tokens are pseudonymous-to-the-operator, not anonymous, and that "this is my own export" is a ToS attestation, not a cryptographic proof.

---

## 2. Adversarial findings folded in as hard requirements

Every CRITICAL and HIGH is a build gate. The MEDIUM/LOW items called out as launch gates are folded in too.

| Sev | Finding | Folded into |
|---|---|---|
| CRIT (R2) | Accept route self-vouch: mutating route gated by the read helper lets a student accept their own request and mint a `verified_vouch` (weight 1.0) | **Slice AE**: mutating routes never use `_intro_access`; explicit `if row['broker_user_id'] != user['id']: raise 403`. Self-vouches can't project a `verified_vouch` edge (**Slice AF**). |
| CRIT (R1) | Importer persists non-consenting third-party PII (operator-reversible token) | **Decision 4 + Slice AB**: RAM-only intersection, zero residue for non-members; PIA + legal opinion is a gate (**Slice AK**). |
| CRIT (R1) | `external_persons` stores cleartext third-party names + unkeyed sha256 | **Decision 3**: `external_persons` removed; imported contacts tokens-only; the only named contact is an employer's own business contact (Slice AF `employer_contacts`), escaped + CSV-injection-neutralized. |
| HIGH (R2) | Pathfinder ignores per-edge `consent_state='shareable'` | **Decision 5 + Slice AD**: single shared helper enforces shareable + not-revoked + both-endpoint consent. |
| HIGH (R2) | Three divergent edge tables | **Decision 1**: unified `graph_edges`. |
| HIGH (R2) | `external_persons` raw CSV names = stored-XSS/CSV-injection sink | **Decision 3 + Slice AF**: tokens-only for imports; output-escape + neutralize leading `= + - @ tab CR` on any human-supplied name before render or CSV export. |
| HIGH (R2) | Single long-lived global pepper in env var; no rotation | **Slice AA**: KMS/HSM MAC op (key never in process memory), **per-school** key, key-version tag on every token, rotation supported; env var only in local-dev behind flag; fail-closed. |
| HIGH (R1) | No retention limits; append-only logs defeat deletion | **Slice Y + AK**: retention TTL columns + scheduled purge; `intro_events` carries no free-text PII (opaque IDs + status only); erasure/anonymization job on account deletion. |
| HIGH (R1) | Soft-delete `deleted_at` contradicts portability | **Decision 3 + Slice Z**: true hard DELETE cascading through edges/vouches/contacts; no soft-delete flag survives. |
| HIGH (R1) | No access/correction/deletion channel for non-members | **Slice Z**: documented data-subject-request path incl. non-member repudiation → permanent `graph_suppressions` tombstone; published privacy notice (Slice AK). |
| HIGH (R1) | Free-text "culture fit" vouch = sensitive-data + proxy vector; subject can't see it | **Slice AF**: reframed to structured, job-related evidence; PII/sensitive scan + redact at ingest; subject can view & contest; employer exposure logged. |
| HIGH (R1) | Fragmented/over-broad consent; double-rebuild collision | **Decision 2 + Slice Y/Z**: one rebuild, four granular independently-revocable purposes. |
| MED (R2) | Import/resolve membership oracle (count leak) | **Slice AB**: `resolve` never returns per-contact counts; min batch size; per-user import rate-limit; anomaly flags on tiny/repeated imports. |
| MED (R2) | `/available` path-exists enumeration oracle; defeats silent decline | **Slice AD**: gate behind an existing application to that posting; coarsen to a single boolean; school-scope `posting_id`; rate-limit; never pre-disclose path existence so decline stays indistinguishable. |
| MED (R2) | Unverified/Sybil vouches earn top weight | **Slice AF**: only `verified_vouch` (coordinator/alumni/employer-verified) projects a high-weight edge; `self_vouch` → low weight; per-voucher rate limit; effective weight capped by voucher verification level. |
| MED (R2) | `application_id` IDOR on intro create | **Slice AE**: verify `application.student_id == requester`. |
| MED (R2) | No per-broker spam throttle | **Slice AE**: per-broker pending-inbound cap per window + broker block/mute + global per-requester live-request cap. |
| MED (R1) | Revocation leaky; backfill/edge-builder resurrect revoked edges | **Slice AC**: edge-builder honors and never overwrites `consent_state='revoked'`; backfill/resolve consult `graph_suppressions` and skip anything tombstoned or predating the subject's consent. |
| MED (R1) | Graph degree allocates opportunity (proxy) | **Slice AI**: governed positive-action program; `NETWORK_FEATURE_KEYS` guard; monitoring + shut-off. |
| MED (R1) | Overlap analytics on non-consenting contacts | Dropped by Decision 4 (no non-member residue → no non-member overlap counts). Any density analytic runs through `MIN_CELL=5` (Slice AH). |
| MED (R1) | Native edges default shareable; both-sides consent | **Slice Y/AC**: `consent_state` default `'pending'`; both endpoints must hold `graph_discoverable` before any traversal/overlap/fairness read counts the edge. |
| LOW (both) | `school_id DEFAULT 1` tenant footgun; PIA/cross-border; inference via `via_mutuals` | **Slice Y**: new Phase-4 tables are `school_id INTEGER NOT NULL` **with no default** + cross-tenant test. `/available` returns a bare boolean (no `via_mutuals`). PIA + data-residency assessment gate in **Slice AK**. |
| LOW (R2) | Free-text length/escaping | **Slices AE/AF**: server-enforced length caps; HTML-escape every render surface. |

---

## 3. Migration 003 — the unified schema

Single file `resume_matcher/stores/migrations/003_phase4.sql`, run by `migrate()` (`db.py:73`). All non-rebuild DDL is `CREATE ... IF NOT EXISTS`; new tenant tables are `school_id INTEGER NOT NULL` (no default); externally-visible rows use `token_urlsafe(10)` PKs; append-only logs use `INTEGER AUTOINCREMENT`; timestamps are `REAL`.

```sql
-- 003_phase4.sql — Phase-4 consented relationship graph + warm-intro pathfinder.
-- SCORING PLANE ONLY: no protected attribute / proxy column (data_planes.PROTECTED_KEYS;
-- enforced by tests/test_platform_db.py). The graph is a SEPARATE surface from match_results:
-- no column here is ever a scoring feature and nothing here is joined into match_results.
-- No contact name is ever stored (Decision 3): there is no `name` column and no external_persons.

-- === (1) SINGLE consents rebuild — the ONE source of truth (Decision 2) ==================
-- consents.purpose carries a hard CHECK; SQLite can't ALTER a CHECK, so rebuild ONCE, adding
-- all four Phase-4 purposes together. This is the ONLY statement in 003 that touches consents,
-- and it is NOT reentrant-by-IF-NOT-EXISTS; migrate() is version-gated (schema_version) to run
-- 003 exactly once on an existing DB. On a fresh DB the copy is 0 rows, so a two-thread first-run
-- race (db.py:78-83) still yields an empty consents — benign.
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
INSERT INTO consents_v2(id,user_id,purpose,version,granted_at,revoked_at)
    SELECT id,user_id,purpose,version,granted_at,revoked_at FROM consents;
DROP TABLE consents;
ALTER TABLE consents_v2 RENAME TO consents;
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id, purpose);

-- === (2) member discovery identity — TOKENS ONLY, no name column =========================
CREATE TABLE IF NOT EXISTS member_graph_identity(
    user_id        INTEGER NOT NULL,
    school_id      INTEGER NOT NULL,
    identity_token TEXT NOT NULL,        -- KMS-MAC(per-school key, canonical_identity); key-versioned
    key_version    TEXT NOT NULL,
    created_at     REAL NOT NULL,
    PRIMARY KEY(user_id, identity_token)
);
CREATE INDEX IF NOT EXISTS idx_member_ident_token ON member_graph_identity(identity_token, school_id);

-- === (3) the ONE canonical edge table (Decision 1) ======================================
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
    weight        REAL NOT NULL DEFAULT 1.0,        -- deterministic traversal-rank convenience; NOT p(hire)
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
    expires_at    REAL                              -- retention TTL (self_upload/derived); NULL = native, decays
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gedges_key ON graph_edges(edge_key);
CREATE INDEX IF NOT EXISTS idx_gedges_a ON graph_edges(user_a, school_id, consent_state);
CREATE INDEX IF NOT EXISTS idx_gedges_b ON graph_edges(user_b, school_id, consent_state);

-- === (4) permanent suppression / tombstone list (revocation durability) =================
-- A deleted/repudiated identity can never be re-materialized by anyone's later import or backfill.
CREATE TABLE IF NOT EXISTS graph_suppressions(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id     INTEGER NOT NULL,
    identity_token TEXT,                            -- suppress by token (non-member repudiation)
    user_id       INTEGER,                          -- suppress by member (account deletion / opt-out)
    reason        TEXT NOT NULL CHECK(reason IN ('member_deleted','member_optout','third_party_repudiation')),
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

-- === (6) posting -> target contact ======================================================
CREATE TABLE IF NOT EXISTS posting_contacts(
    id                TEXT PRIMARY KEY,
    school_id         INTEGER NOT NULL,
    posting_id        TEXT NOT NULL,
    contact_user_id   INTEGER,                      -- hiring manager as a member (usual: posting.created_by)
    employer_contact_id TEXT,                       -- or a business contact (employer_contacts.id)
    relation          TEXT NOT NULL DEFAULT 'hiring_manager'
        CHECK(relation IN ('hiring_manager','recruiter','referrer','team_member')),
    added_by          INTEGER NOT NULL,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posting_contact_posting ON posting_contacts(posting_id);

-- === (7) vouches — structured, job-related evidence (NOT "culture fit") =================
CREATE TABLE IF NOT EXISTS vouches(
    id               TEXT PRIMARY KEY,
    school_id        INTEGER NOT NULL,
    voucher_user_id  INTEGER NOT NULL,
    subject_user_id  INTEGER NOT NULL,              -- vouched-for member (Decision 3: always a user)
    scope            TEXT NOT NULL DEFAULT 'general'
        CHECK(scope IN ('general','posting','org')),
    posting_id       TEXT,
    org_id           INTEGER,
    -- STRUCTURED job-related fields (bounded enums), preferred over prose:
    relationship     TEXT CHECK(relationship IN
                       ('worked_together','managed_them','ta_instructor','classmate','mentored_them','other')),
    evidence_redacted TEXT,                          -- short free text, PII/sensitive-scanned & redacted AT INGEST
    verify_level     TEXT NOT NULL DEFAULT 'self'
        CHECK(verify_level IN ('self','coordinator','alumni_verified','employer_verified')),
    verified_by_user_id INTEGER,
    verified_at      REAL,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','withdrawn','contested')),
    contested_note   TEXT,                           -- subject's contest, if any
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vouches_subject ON vouches(subject_user_id, status);
CREATE INDEX IF NOT EXISTS idx_vouches_voucher ON vouches(voucher_user_id, status);
CREATE INDEX IF NOT EXISTS idx_vouches_posting ON vouches(posting_id);

-- === (8) double-opt-in intro lifecycle ==================================================
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

-- === (9) broker abuse controls =========================================================
CREATE TABLE IF NOT EXISTS broker_blocks(
    broker_user_id  INTEGER NOT NULL,
    blocked_user_id INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY(broker_user_id, blocked_user_id)
);
```

Second place for the consent change: append the four purposes to `CONSENT_PURPOSES` in `students.py:22`. `set_consent`/`has_consent`/`consents()` (`students.py:68-98`) then work unchanged.

No `db.py` change is needed — dropping the file registers it (`db.py:73`). The consents rebuild is the **only** non-idempotent statement; it must not be duplicated elsewhere in 003.

---

## 4. Cross-cutting infrastructure (referenced by slices)

**Stores** (all subclass `_Store`, Pattern A, `engage.py:24-30`; per-call `with closing(self._conn())`; parameterized SQL; own `*Error`):
- `stores/graph.py` — `NetworkStore` (import, discovery identity, resolve, delete) + `GraphError`.
- `stores/relationships.py` — `RelationshipStore` (edge upsert/consent, edge-builder, the shared traversal helper) + `RelationshipError`.
- `stores/intros.py` — `IntroStore` (ranking pure-functions, pathfinder, intro lifecycle) + `IntroError`.

**Route prefixes** added to `_PLATFORM_PREFIXES` (`auth.py:43-48`): `/api/network`, `/api/graph`, `/api/intros`, `/api/vouches`. Each route declares its own `Depends(require_role(...))`.

**The single shared traversal predicate** (used by pathfinder, overlap, fairness — Decision 5), implemented once in `RelationshipStore`:

```python
_SHAREABLE = (
    "e.consent_state='shareable' AND e.revoked_at IS NULL "
    "AND (e.expires_at IS NULL OR e.expires_at > :now) "
    "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=e.user_a "
    "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
    "AND EXISTS(SELECT 1 FROM consents c WHERE c.user_id=e.user_b "
    "          AND c.purpose='graph_discoverable' AND c.revoked_at IS NULL) "
    "AND NOT EXISTS(SELECT 1 FROM graph_suppressions s "
    "          WHERE s.user_id IN (e.user_a,e.user_b))")
```

Both endpoints consented; edge is shareable, live, un-tombstoned. No read path may bypass this helper.

---

## 5. The slices (restartable, each independently shippable & green)

Each slice lists **files**, **tests**, and a **shippable exit**. Slices Y–AC carry no user-visible surface and can land ahead of the UI.

### Slice Y — Migration 003 + unified consent rebuild + tenant hardening
- **Files:** `stores/migrations/003_phase4.sql` (§3); `students.py:22` (`CONSENT_PURPOSES` += 4 purposes).
- **Tests:** `test_platform_db.py` still green (no protected columns, no allowlist edit — assert it). New `test_migration_003_idempotent_on_fresh_and_existing` (apply twice; consents rebuild preserves rows on existing DB, empty on fresh). `test_consents_purposes_match_check` (Python tuple ⇔ SQL CHECK). `test_phase4_tables_school_id_not_null` (INSERT with NULL school_id fails). `test_cross_tenant_no_default` (a store call omitting school_id raises, not silently pools to 1).
- **Exit:** migration applies cleanly forward on a populated `data/platform.db`; CI green behind `RM_PLATFORM_ENABLED`.

### Slice Z — Consent store, granular API, and data-subject request (DSR) path
- **Files:** `stores/graph.py` (`GraphConsentStore` wrapping the four purposes via `set_consent`/`has_consent`); `api/platform.py` new routes:
  - `GET/POST /api/graph/consents` — grant/revoke each granular purpose (`require_role()` any signed-in user).
  - `DELETE /api/network` — member erasure (Slice AB/AC cascade + tombstone).
  - `POST /api/graph/repudiate` — **non-member** DSR: given a self-asserted identity, tokenize in RAM, insert a `graph_suppressions(identity_token, reason='third_party_repudiation')`, and hard-delete any edge that resolved to them. Public-facing, rate-limited, no auth (a non-member has no account) but CAPTCHA-free per policy — instead throttled + logged.
- **Hard requirements folded in:** revoking `graph_discoverable` immediately removes the member from every read path (the shared helper checks it live); erasure is a true hard DELETE (no `deleted_at`), and it writes a permanent `graph_suppressions(user_id, reason='member_deleted')` so backfill can never resurrect them.
- **Tests:** `test_revoke_hides_member_everywhere` (traversal, overlap, fairness all drop the user in one revoke). `test_dsr_repudiation_tombstones_and_deletes`. `test_erasure_is_hard_delete`.
- **Exit:** a member can grant/revoke each purpose independently; a non-member can be suppressed; both are durable against re-import.

### Slice AA — Tokenizer (KMS-MAC, per-school key, versioned, fail-closed)
- **Files:** `stores/graph_tokens.py`: `canonical_identity(first,last,company)` (NFKC + diacritic strip + casefold + company-suffix strip, `\x1f`-joined — verbatim from Component 2 §3a); `identity_token(school_id, first, last, company) -> (token, key_version)`.
- **Hard requirements folded in (R2-HIGH pepper):** production computes the MAC via a **KMS/HSM MAC operation** so the key never enters process memory; the key is **per-school** (limits cross-tenant linkage); every token is stored with its `key_version` so rotation re-tokenizes incrementally without a full re-import. Local-dev fallback: `RM_GRAPH_PEPPER` env var, permitted **only** when `RM_ENV=dev`. Missing key material → `GraphError`, importer disabled (fail-closed). Pepper/key leak is a reportable-breach item in the incident plan (Slice AK).
- **Tests:** pure unit tests — canonicalization determinism (`José`→`jose`, `Acme, Inc.`→`acme`), stability, per-school divergence (same identity, different school → different token), key-version tagging, fail-closed with no key.
- **Exit:** deterministic, per-school, versioned tokens; no key in app memory in prod.

### Slice AB — Contacts importer (RAM-only PSI-lite; zero non-member residue)
- **Files:** `stores/graph.py` `NetworkStore.import_csv`; `POST /api/network/import` (`require_role("student")`, multipart) → enqueues `resolve_network`, returns `202` + poll URL.
- **Flow (Decision 4):** require active `contacts_upload` consent; stream the multipart part under a **5 MB / 30 000-row** byte cap *before* decode; skip the LinkedIn preamble (find the `First Name` header line); `utf-8-sig`→`latin-1` fallback; tokenize every row in RAM. **Intersect the token set against `member_graph_identity` for consenting members in the same school, entirely in RAM. Only intersection hits proceed; every non-matching token is discarded before any commit.** The raw CSV bytes are never written; no `network_contacts` table exists.
- **Hard requirements folded in:**
  - No non-member token is ever persisted (C1).
  - `resolve` returns **no per-contact counts** to the uploader (R2 membership-oracle) — the response is only job status; edges are actionable solely via the other party's double-opt-in.
  - Minimum batch size (reject 1–2 row uploads), per-user import rate-limit, and anomaly flags on tiny/repeated imports (R2 oracle).
  - Consent text carries the ownership attestation (ToS, not proof — Boundary #4 honesty).
- **Tests:** `test_import_discards_non_members` (upload 100 contacts, 3 members → exactly 3 candidate edges, 0 residual tokens in DB). `test_import_no_count_egress`. `test_import_size_and_batch_guards`. `test_import_fail_closed_without_key`.
- **Exit:** a member can upload their own export; only consenting-member intersections survive; non-members leave zero residue.

### Slice AC — Edge builder + resolve/backfill (revocation-durable)
- **Files:** `stores/relationships.py` `RelationshipStore.upsert_edge` / `build_native_edges` / `resolve_import` / `backfill_on_optin`; `@register_handler("build_edges")` and `@register_handler("resolve_network")` (mirror `_match_posting_job`, `platform.py:548`).
- **Native folding:** `event_registrations`→`event_coattendance`, `messages`(app thread)→`message_thread`, accepted `interview_slots`→`interview`, `applications`→`application`. `edge_key` (UNIQUE) makes upserts idempotent (`ON CONFLICT(edge_key) DO UPDATE`).
- **Hard requirements folded in (R1/R2 revocation & consent):**
  - Native edges default `consent_state='pending'` (**not** shareable); an edge becomes `'shareable'` only when the owner opts a source in, and it is traversable only when **both** endpoints hold `graph_discoverable` (shared helper).
  - The builder **never overwrites `consent_state='revoked'`** and **skips any endpoint in `graph_suppressions`** — a re-run can't resurrect a revoked or tombstoned edge.
  - Backfill on a new opt-in only activates edges whose underlying interaction post-dates the subject's own consent (no pre-consent data resurrection).
  - Self-upload/derived edges get `expires_at = now + retention TTL` (Slice AK).
- **Tests:** `test_edge_builder_idempotent`. `test_builder_never_unrevokes`. `test_builder_skips_suppressed`. `test_backfill_ignores_preconsent_interactions`. `test_native_edge_default_pending`.
- **Exit:** the graph materializes from consented sources; revocation and suppression are honored across re-runs.

### Slice AD — Pathfinder (consent-gated BFS + verified-vouch ranking)
- **Files:** `stores/intros.py`: `EDGE_STRENGTH`, `edge_score`, `rank_path`, `path_sort_key`, `IntroStore.find_paths` (bounded BFS, `MAX_DEPTH=3`, `TOP_K=5`, product-of-edge-scores, recency half-life 180d). `find_paths` uses the shared `_SHAREABLE` predicate for **every** neighbour expansion.
- **`EDGE_STRENGTH` (Sybil/verification fix folded in):**
  ```
  verified_vouch 1.00 | interview 0.85 | message_thread 0.70 | application 0.55
  event_coattendance 0.45 | alumni_bridge 0.40 | self_vouch 0.15 | linkedin_connection 0.30
  ```
  `self_vouch` sits at the floor (0.15); only `verified_vouch` earns 1.00. Effective vouch weight is capped by the voucher's `verify_level` (Slice AF).
- **`GET /api/intros/available/{posting_id}`** (`require_role("student")`) — folded R2 oracle fixes: **gated behind an existing application** to that posting; `posting_id` scoped to the caller's `school_id`; returns a **bare boolean** `{"warm_intro_available": bool}` (no `hops`, no `via_mutuals`); per-requester rate-limited. Path existence is never granularly disclosed, so a later silent decline stays indistinguishable.
- **Tests:** pure ranking unit tests (product beats a strong+stale sum; hop tie-break; recency floor). `test_pathfinder_respects_shareable_and_both_consent` (pending/revoked edge never walked; far-endpoint-only consent not enough). `test_available_requires_application_and_is_boolean`. `test_pathfinder_no_audit_import` (grep: module never imports `audit_store`/`data_planes`).
- **Exit:** a consented path can be found; every non-consented or non-shareable edge is invisible; the student surface leaks only a boolean.

### Slice AE — Double-opt-in intro flow (authz-hardened)
- **Files:** `stores/intros.py` lifecycle methods; `api/platform.py` routes; `_intro_access(intro_id, user)` **read-only** helper (coordinator/admin, or broker, or requester; else 404 — no existence leak, mirrors `_application_access` `platform.py:679-692`).
- **Routes:**
  - `POST /api/intros/requests` (`require_role("student")`) — **IDOR fix:** verify `application.student_id == requester` before use; `application_id` is required. Picks best path, sets `broker = path.nodes[1]`, `409` on `UNIQUE(requester,posting)`. **Spam fix:** reject if the broker is at their per-window pending-inbound cap, if the requester is at their global live-request cap, or if `broker_blocks` contains (broker, requester). Student response is identity-blind.
  - `GET /api/intros/inbox` (`require_role()`) — broker's own `status='requested'` rows; the opt-in reveal of requester+role.
  - `POST /api/intros/requests/{id}/accept` — **CRITICAL fix:** explicit `if row['broker_user_id'] != user['id']: raise HTTP 403` (does **not** use `_intro_access` for authorization). Writes the vouch via Slice AF, ensures the `applications` row, `requested→accepted`, reveals broker↔student.
  - `POST /api/intros/requests/{id}/decline` — same explicit broker-only check. `requested→declined`; requester sees only the neutral "no intro available" (silent decline).
  - `POST /api/intros/broker/block` (`require_role()`) — broker mutes a requester.
  - `GET /api/intros/requests/mine` (`require_role("student")`) — broker identity/vouch surfaced only when `status='accepted'`.
- **Folded requirements:** `note_redacted` server-enforced ≤500 chars + PII-redacted at ingest + HTML-escaped on render; `intro_events` records only status transitions (no free-text); every transition appends there (compliance replay, `posting_events` pattern). `expires_at` sweep sets `expired`; `purge_after` set on terminal status for the retention job.
- **Tests:** `test_student_cannot_accept_own_request` (the CRITICAL). `test_intro_idor_blocked`. `test_broker_pending_cap_and_block`. `test_decline_is_silent_to_requester`. `test_intro_events_have_no_freetext`.
- **Exit:** a genuine mutual can broker a double-opt-in intro; no student can self-accept; spam and IDOR are blocked.

### Slice AF — Vouches as structured, job-related evidence
- **Files:** `stores/relationships.py` `create_vouch` / `verify_vouch` / `contest_vouch`; `api/vouches` routes; the ingest redaction chokepoint.
- **Folded requirements (R1-HIGH culture-fit + R2 Sybil):**
  - Reframed away from "culture fit": input is the structured `relationship` enum + a short `evidence` free-text. The free-text is **PII/sensitive-attribute scanned and redacted at ingest, before persistence** (stored as `evidence_redacted`) — this is a new, earlier chokepoint than the pre-LLM one, and it also runs before any LLM (Boundary #3).
  - Only `verify_level IN ('coordinator','alumni_verified','employer_verified')` projects a `verified_vouch` edge; `self` projects a low-weight `self_vouch` edge. Effective traversal weight is capped by `verify_level`.
  - Per-voucher rate limit per window (anti-Sybil); a vouch's edge is created in the same txn (`provenance='vouch'`).
  - **Subject rights:** `GET /api/vouches/about-me` lets the subject view every vouch about them; `POST /api/vouches/{id}/contest` sets `status='contested'` and records `contested_note`; contested vouches are excluded from traversal until resolved.
  - Employer exposure is logged via a `shortlist_exposed`-style append-only event (`platform.py:585-600`).
  - Any human-supplied label rendered anywhere is HTML-escaped and CSV-injection-neutralized (leading `= + - @ tab CR`).
- **Tests:** `test_self_vouch_low_weight_no_verified_edge`. `test_vouch_ingest_redacts_pii`. `test_subject_can_view_and_contest`. `test_vouch_rate_limit`. `test_contested_vouch_not_traversed`.
- **Exit:** vouches are structured, verified-tiered, redacted, subject-contestable, and exposure-logged.

### Slice AG — Employer evidence card
- **Files:** `GET /api/intros/for-application/{app_id}` (`require_role("employer","coordinator","admin")`, via `_application_access`); render on the existing applicant view (`ApplicationStore.for_posting`, `students.py:194-212`).
- **Folded requirements:** the card shows the accepted vouch as **quoted, attributable, job-related evidence** with `claim_kind='job_related_evidence_not_hire_recommendation'`; the per-edge provenance quotes from `rank_path` explain why the voucher is credible ("co-attended York AI Career Fair 2026"). The vouch is **never** added to `match_results.result_json` and never touches `rank_path` scoring inputs.
- **Tests:** `test_intro_card_not_in_match_results` (schema + `result_json` keys carry no `intro`/`vouch`/`degree`/`connector`). `test_card_output_escaped`.
- **Exit:** employers see warm-intro evidence beside — never inside — the fit/readiness score.

### Slice AH — Fairness audit report (aggregate-only, MIN_CELL=5)
- **Files:** `audit/metrics.py` new `access_disparity` + `AccessDisparity` (Component 4 §2, verbatim); `api/platform.py` `_intro_cohort_refs` + `GET /api/coordinator/reports/intro-equity` (`require_role("coordinator","admin")`, JSON+CSV, mirrors `self_id_report` `platform.py:853-869`).
- **Two funnels:** intro **access** (who received an intro at all) and intro **conversion** (whose intro was accepted / led to shortlisted+). Each computed per `AUDITABLE_ATTRIBUTES` from **two independent `AuditDB.aggregate(refs, attr)` calls** (numerator, denominator), never an aligned per-person label list.
- **Folded requirements:** suppressed-numerator ≠ zero (report "below reporting threshold", never 0 — the privacy nuance); four-fifths badge reuses the `selection_audit` threshold; `<2` surviving groups → `four_fifths_pass=None`; the two DB connections never span both planes (`db_connect` for refs, `AuditDB` for labels — structural guarantee `audit_store.py:4-5`); overlap density analytics (if any) also run through `MIN_CELL=5`.
- **Tests:** unit tests on synthetic count dicts (pass/fail, suppressed-numerator, single-group None); `test_intro_equity_two_connections_never_joined`.
- **Exit:** a coordinator can see whether warm intros concentrate among privileged groups, provably and without per-person joins.

### Slice AI — Active mitigation as a governed positive-action program
- **Files:** `stores/data_planes.py` new `NETWORK_FEATURE_KEYS = {"degree","reachability","intro_count","connector_count","components"}` + an assertion in the scoring path (extends `assert_no_protected`, `data_planes.py:44-51`); `_mitigation_coverage(school)`; alumni-bridge matcher + coordinator-initiated intro action (reuse `_notify_creator`, `platform.py:873-881`).
- **Folded requirements (R1-MED graph-degree-as-proxy):**
  - The no-proxy guarantee is extended to **all** opportunity surfaces: `NETWORK_FEATURE_KEYS` may never enter a `JobSpec`/`CandidateProfile` feature dict (CI test).
  - `network_poverty` (structural: `consented_degree==0 OR reachable_live_postings==0`) is the **only** mitigation trigger — **never self-ID.**
  - The mitigation is documented as an explicit, reviewed **positive-action program** with a legal basis, monitoring, and a **shut-off** if it fails to close the access gap. `_mitigation_coverage` reports before/after impact ratios (all intros vs `origin='organic'` only) and the **conversion** funnel, so hollow "pass the ratio" intros are visible.
  - Alumni bridges and coordinator-initiated intros still require the far party's double-opt-in (no cold outreach — consistent with `002:31-38`).
- **Tests (the §5 boundary trio):** `test_network_features_rejected_by_scoring_plane`; `test_intros_not_in_match_results`; `test_pathfinder_and_mitigation_no_audit_import`.
- **Exit:** the platform can *close* the network gap for structurally under-networked students, provably, without letting degree become a scoring proxy.

### Slice AJ — UI wiring
- **Files:** `student.html` (granular consent screen with ownership attestation; contacts upload widget; discovery opt-in; "delete my network"; "vouches about me" view/contest; identity-blind "request a warm intro" behind an application). `coordinator.html` (intro-equity card beside funnel + self-ID cards; mitigation coverage). Employer applicant view (evidence card, Slice AG). Broker inbox surface.
- **Folded requirements:** every rendered human-supplied string is HTML-escaped; consent toggles map 1:1 to the four granular purposes; no surface exposes `via_mutuals` or per-contact resolution counts.
- **Tests:** route smoke tests + an output-escaping test per surface.
- **Exit:** the full flow is usable end-to-end behind `RM_PLATFORM_ENABLED`.

### Slice AK — Retention/erasure job + legal & privacy gates
- **Files:** `@register_handler("graph_retention")` scheduled purge; `workers/runner.py` registration; `docs/DESIGN.md` + a new privacy notice.
- **Folded requirements (R1-HIGH retention; both-reviewer legal):**
  - **Retention schedule:** self-upload/derived `graph_edges` purged at `expires_at` (default 12 months); `intro_requests` hard-deleted at `purge_after` (terminal + 6 months); native edges recomputed/decayed, no free-text to retain; `member_graph_identity` cleared on discovery opt-out.
  - **Erasure job** on account deletion cascades: hard-delete the member's edges, vouches (as voucher and as subject), intro rows, discovery identity; write `graph_suppressions(user_id, 'member_deleted')`; `intro_events` retain only opaque IDs + status (no PII to scrub).
  - **Gates that must clear before this feature leaves the flag:** a documented PIA (FIPPA — mandatory for a public university), a legal opinion on the LinkedIn self-export lawful basis (PIPEDA consent / FIPPA indirect-collection, business-contact exemption for `employer_contacts` only), a data-residency/cross-border assessment confirming the local-backend commitment covers all PII including vouch text, and the pepper/key-leak reportable-breach runbook.
- **Tests:** `test_retention_purges_expired`. `test_erasure_cascade_leaves_no_pii`. `test_intro_events_pii_free_after_erasure`.
- **Exit:** retention is enforced, erasure is true and complete, and the documented legal gates are recorded as launch blockers.

---

## 6. What we deliberately do NOT store

- **No raw LinkedIn CSV** — parsed in RAM, discarded; no blob, no re-download endpoint.
- **No name or company text for any imported contact** — tokens only, and only for resolved consenting members. There is no `network_contacts` table and no `external_persons` table.
- **No token for any non-member** — non-matching tokens are discarded in RAM before commit (zero residue).
- **No contact-list membership or overlap counts about non-members** — the membership oracle and non-member overlap analytics are designed out.
- **No `via_mutuals` / hop count / path detail to the requesting student** — only a bare boolean, gated behind an application.
- **No per-import resolution count returned to the uploader.**
- **No free-text PII in append-only logs** (`intro_events` is status + opaque IDs only).
- **No un-redacted vouch/evidence text** — PII/sensitive-scanned and redacted at ingest, before persistence and before any LLM.
- **No protected attribute or proxy in any scoring or opportunity-allocation surface** — not in `match_results`, not in a scoring feature dict, and `NETWORK_FEATURE_KEYS` (graph degree/reachability) are barred from scoring.
- **No `deleted_at` soft-delete for people data** — deletion is a hard DELETE plus a permanent suppression tombstone.
- **No pepper/key in application process memory in production** — KMS/HSM MAC, per-school, key-versioned; env-var key only in `RM_ENV=dev`.
- **No `school_id` default on Phase-4 tables** — `NOT NULL`, explicit, cross-tenant-tested.

---

## 7. Build order & flag posture

Land Y→AC (no user surface) first; they establish the schema, consent source-of-truth, tokenizer, importer, and edge graph. AD→AG deliver the pathfinder + intro + vouch + employer card. AH→AI deliver the provable-fairness differentiator and its governed mitigation. AJ wires the UI; AK enforces retention and records the legal launch gates. Everything ships behind `RM_PLATFORM_ENABLED`, so `RM_PLATFORM_ENABLED=0` preserves the pre-Phase-4 posture at all times. The CRITICAL and HIGH fixes above are merge-blocking; the MEDIUM launch-gate fixes (oracles, Sybil, IDOR, spam) must be green before the flag is turned on for any real school, and the Slice AK legal gates (PIA, legal opinion, residency assessment) are the final blockers before general availability.