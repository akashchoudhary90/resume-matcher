# Design rationale & load-bearing decisions

This document is the durable, in-repo record of *why* the architecture is shaped the way it is. It
exists so the receiving student team can extend the tool without accidentally removing a guardrail
that looks like incidental complexity but is actually load-bearing. **Do not relax any item below
without an explicit, documented decision** (ideally an ADR under `docs/adr/`).

## What this tool is

A York-internal, consent-first tool that matches Handshake job postings to the students most likely
to succeed at getting them: LLM resume scoring, transferable-skill matching, a **bias-audit
dashboard**, and defenses against gamed / AI-generated resumes. It is built to be handed off to a
3–4 person student team and, eventually, hosted on York-controlled infrastructure.

## The four load-bearing boundaries (do not violate)

1. **No LinkedIn scraping.** Only "Sign in with LinkedIn (OpenID Connect)" + student self-upload, or
   the coordinator's *sanctioned* Handshake bulk export. Scraping is a legal non-starter (hiQ;
   PIPEDA/FIPPA).
2. **Protected attributes / proxies NEVER enter scoring.** Enforced by two physically separated data
   planes (`stores/data_planes.py`). The "hire from our own community" hunch is implemented *only* as
   the **homophily-disparity audit metric** — a detect-and-flag signal, never an encoded feature.
3. **PII stays local by default.** A mandatory redaction pass (`inference/redaction.py`) runs before
   any non-local adapter sees text. The redaction tripwire (`assert_redacted`) gates the
   `is_local=False` path in `inference/adapter.py`.
4. **No fabricated "% chance of hire."** We ship an honest **fit/readiness score** with a
   point-by-point breakdown — every point is tied to a skill the job asked for and a **verbatim
   quote** from the resume. Full credit requires a quote that DEMONSTRATES the skill; two explained
   half-credit paths exist: **adjacency credit** (the quote demonstrates a curated ADJACENT skill —
   `data/skill_relations.json`; the LLM proposes, the deterministic ranker verifies the quote
   contains that skill and refuses anything off-graph; flagged `adjacent_credit`) and
   **named-is-not-demonstrated** (a quote that merely names the skill earns half weight; flagged
   `bare_mention`). **The LLM never makes the scoring decision**; the deterministic ranker does
   (`matching/ranker.py`), and it verifies each evidence quote against the resume text (this is
   what makes the pipeline injection-resistant). Every line item's note states which rule applied,
   and the breakdown reconciles exactly to the headline number.

## Inference is swappable on purpose

All model access goes through one `InferenceAdapter` (`inference/adapter.py`), selected by
`RM_INFERENCE_BACKEND`: `mock`, `claude_code`, `claude_cli`, `ollama`, `openai_compat`. The default
is the local Claude Code CLI (`claude_cli`) on the user's subscription — no paid API. The receiving
team can drop in their own backend with a one-line config change. **When adding a backend, set
`is_local` correctly** — `is_local=False` forces the redaction tripwire; getting this wrong leaks
PII off-box.

## The ephemeral real-data demo

The client-facing "try it with your own data" flow (`api/demo.py`, `/api/demo/*`) processes uploads
**in memory only, never on disk**, drops the full resume text the instant scoring finishes, keeps
only the de-identified breakdown, auto-expires sessions after an idle TTL, and offers an explicit
"Delete my data now". Clients consent to uploading PII, so the demo optimizes for results/UX (keeps
the applicant name, labels by filename) while still redacting contact identifiers. With the Claude
engine, consented resume text does go to Anthropic via the subscription session (a known gray-area
ToS tradeoff, mitigated by ephemerality).

## Where to go next

- `README.md` — quick start, the boundaries, the API contract.
- `DEPLOY.md` / `deploy/cohost/COHOST.md` — deployment and the self-contained co-hosted demo stack.
- `PRIVACY.md` — the privacy posture in user-facing terms.
