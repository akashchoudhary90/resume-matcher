# Resume Matcher — York Career-Services Job ↔ Student Matching & Coaching

A **York-internal, consent-first** tool that matches Handshake job postings to the students
most likely to succeed at getting them, with LLM resume scoring, transferable-skill matching,
a **bias-audit dashboard**, and defenses against gamed / AI-generated resumes.

It runs on a **swappable local LLM** (default: local Claude Code, no paid API) so the receiving
team can drop in their own backend later with a one-line config change.

> Full design rationale, legal boundaries, and the 12-week roadmap live in the approved plan:
> `C:\Users\akash\.claude\plans\form-now-the-problem-wise-karp.md`.

---

## The four load-bearing boundaries (do not violate)

1. **No LinkedIn scraping.** Only "Sign in with LinkedIn (OpenID Connect)" + student self-upload.
2. **Protected attributes / proxies NEVER enter scoring.** Enforced by two physically separated
   data planes (`stores/data_planes.py`).
3. **PII stays local by default.** A mandatory redaction pass (`inference/redaction.py`) runs before
   any non-local adapter ever sees text.
4. **No fabricated "% chance of hire."** We ship an honest **fit/readiness score** with visible
   components, and the LLM never makes the scoring decision — a deterministic ranker does
   (`matching/ranker.py`).

---

## Quick start

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# bash:                source .venv/bin/activate

pip install -r requirements.txt          # minimal core (runs the full demo + tests)
pip install -r requirements-extra.txt    # optional: real embeddings, Fairlearn, MCP, API, UI

python scripts/gen_synthetic.py          # write synthetic resumes + postings to data/synthetic/
python scripts/run_demo.py               # end-to-end: ingest -> match -> rank -> coach -> audit
pytest -q                                # unit + contract + injection + audit + e2e smoke tests
```

The demo and tests run with **only** the core requirements — heavy/optional packages light up
enhanced behavior (real sentence-transformer embeddings, Fairlearn, a live LLM backend) when present.

### Web app (browser dashboard + JSON API)

```bash
pip install fastapi uvicorn httpx          # (or: pip install -r requirements-extra.txt)
uvicorn resume_matcher.api.app:app --reload
# open http://127.0.0.1:8000  → click "Load synthetic data"
```

The dashboard ([resume_matcher/api/static/index.html](resume_matcher/api/static/index.html)) is a thin,
no-build-step client over the `/api/*` endpoints — a coordinator can browse per-job shortlists with fit
scores + review flags, look up a student's closest-fit roles, and read the live bias-audit panel. The
API is the contract (see `/docs`); a React front-end can replace the bundled HTML later. Key endpoints:
`POST /api/load-synthetic`, `GET /api/jobs`, `GET /api/jobs/{id}/shortlist`, `GET /api/candidates/{id}`,
`GET /api/audit`, `POST /api/score`.

**Admin password:** set `RM_ADMIN_PASSWORD` (and optionally `RM_ADMIN_USER`, default `admin`) to put
the whole app — dashboard, API, and docs — behind HTTP Basic auth. Unset = open (local dev only).

### Deploying behind a domain (Docker + automatic HTTPS)

```bash
export RM_ADMIN_PASSWORD='choose-a-strong-password'
docker compose up -d --build      # app + Caddy (auto Let's Encrypt TLS)
```

Point a DNS A record at the server and Caddy serves it over HTTPS. Full walkthrough (DNS, AWS security
group, env vars, and the **synthetic-data-only** boundary) in **[DEPLOY.md](DEPLOY.md)**.

**Co-hosting on a box that already runs Caddy** (e.g. an existing VPS): use the isolated,
resource-capped stack in [deploy/cohost/](deploy/cohost/) — it runs only the app, joins a dedicated
`proxy_edge` network, and is routed by the existing Caddy on a new subdomain without touching the other
service. Runbook: **[deploy/cohost/COHOST.md](deploy/cohost/COHOST.md)**.

---

## Swapping the LLM backend (the handoff knob)

Everything depends on one narrow interface, `InferenceAdapter.extract()` — never on a model SDK.
Pick a backend with a single environment variable; **no code changes**:

```bash
RM_INFERENCE_BACKEND=mock          # deterministic, dependency-free (default in CI / tests)
RM_INFERENCE_BACKEND=claude_code   # local Claude Code via MCP sampling (default for dev)
RM_INFERENCE_BACKEND=ollama        RM_OLLAMA_MODEL=qwen2.5:7b-instruct
RM_INFERENCE_BACKEND=openai_compat RM_OPENAI_BASE_URL=... RM_OPENAI_MODEL=... RM_OPENAI_API_KEY=...
```

The contract test (`tests/test_adapter_contract.py`) runs the same fixtures through more than one
adapter and asserts schema-valid output — that is the proof of swappability.

---

## Repository layout

```
resume_matcher/
  inference/    InferenceAdapter + adapters, MatchExtraction/ScoreResult schema, redaction, MCP server
  ingestion/    Handshake-export importer, resume parser, synthetic-data generator
  matching/     skill taxonomy, retrieval, rerank, LLM evaluator, deterministic ranker, coaching
  audit/        bias-audit metrics (4/5ths + Fisher + rank-aware + homophily), proxy-leakage test
  antigaming/   hidden-text detection, keyword-stuffing checks, prompt-injection detection
  stores/       scoring_store + audit_store — two physically separated data planes
  api/          FastAPI wiring (optional)
  ui/           Streamlit coordinator dashboard (optional)
scripts/        gen_synthetic.py, run_demo.py
tests/          unit + contract + injection + audit + e2e smoke
```

---

## Status

This is the **Phase 0 + skeleton** deliverable from the roadmap: repo scaffold, shared contracts,
three swappable adapters, synthetic data, a runnable end-to-end pipeline, and the test suite that
proves the architectural properties (swappability, injection-resistance, bias auditability).
Real Handshake ingestion runs only on **sanctioned, consented data after privacy sign-off** — develop
on synthetic data until then.
