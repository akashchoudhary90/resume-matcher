# Privacy & data handling — the "try it with your own data" demo

This describes exactly what happens to the files you upload on the **`/demo`** page so you can decide,
with full information, whether to try the tool with real resumes. It reflects how the code actually
behaves (`resume_matcher/api/demo.py`, `resume_matcher/ingestion/parser.py`,
`resume_matcher/inference/redaction.py`) — not a marketing summary.

## What you upload

- **One job posting** (pasted text and/or title/employer) and **up to 10 resumes** (`.pdf`, `.docx`,
  or `.txt`, up to 4 MB each).

## What happens to it

1. **Processed transiently, not stored.** Uploaded files are read into RAM and parsed there; the
   demo does not persist resume files, extracted text, or results to any file or database.
   (`test_run_demo_writes_nothing_to_disk` fails the build if the deterministic path writes anything
   to disk.) **One exception:** when the **Claude engine reads a PDF/image directly**, a temporary
   copy of that file is written so the local Claude CLI can open it, then **deleted immediately**
   after scoring. Nothing is retained either way.
2. **Contact identifiers are redacted at ingestion; the data is treated as real PII otherwise.**
   Before scoring, a pass replaces email, phone, URLs, street address, and postal code with typed
   placeholders, and strips hidden/zero-width characters. The candidate's **name is kept** (results
   are labelled by the uploaded filename so they're identifiable) — this is real personal data you
   are choosing to process. The protection here is **ephemerality + deletion** (below), not
   anonymization.
3. **The full resume text is dropped the instant scoring finishes.** The only thing kept for your
   session is the **de-identified score breakdown** — the fit score, the per-skill points, and short
   evidence quotes (each a few words, already passed through redaction). The full resume text is not
   retained anywhere after scoring.
4. **Processing is local.** By default the matching engine is deterministic and runs entirely on the
   server (`RM_DEMO_BACKEND=mock`); your resume text is not sent to any third-party API.

## How it is deleted

- **You can delete it immediately.** The results page has a **"Delete my data now"** button. It calls
  `DELETE /api/demo/session/{id}`, which wipes the in-memory session right away.
- **It auto-deletes when idle.** Each session expires after an idle timeout (default **30 minutes**,
  `RM_DEMO_TTL_MINUTES`). A background task and every access purge anything past its TTL.
- **A restart erases everything.** Because sessions live only in process memory, restarting the app
  (or `docker compose down`) destroys all sessions.

## What we do NOT do

- We do **not** store your resumes or results on disk or in a database.
- We do **not** use protected attributes (race, gender, etc.) anywhere in scoring — the bias features
  are a *separate, audit-only* data plane and are never an input to the score.
- We do **not** send your data to a third-party model by default.

## Scope & honest limitations

- This is a **demo** posture for evaluating the tool with sample data. For ongoing use with real
  student records, the data must live on a governance-cleared, institution-controlled host with
  formal consent and retention policies — not a demo box.
- Redaction is best-effort. If a resume's body text names the candidate, that name may appear in a
  short evidence quote. Delete your session when you're done.

Questions or a deletion request beyond the in-app button: contact the operator who shared this demo
link with you.
