# Defense File — open verification spec (v1)

A **Defense File** is a tamper-evident, reproducible, signed record of how a set of fit-readiness scores
was derived. It exists so that a third party — a regulator, an auditor, a candidate, or opposing counsel
— can **verify** a hiring screen's reasoning instead of trusting the vendor's word. This document is the
open spec: anyone can implement an independent verifier from it, with no dependency on the issuer.

It is honestly **tamper-evident and reproducible** — it is *not* a claim of court-admissibility, and the
score it records is a **fit/readiness** score, explicitly **not a probability of hire**.

## File shape

```jsonc
{
  "format": "resume-matcher-defense-file",
  "version": 1,
  "generated_at": 1782599999.0,
  "engine": "claude_cli",                 // which matching engine extracted the evidence
  "sig_alg": "ed25519",                   // or "hmac-sha256" (fallback)
  "public_key": "<hex>",                  // Ed25519 public key the records are signed with (null for HMAC)
  "salt": "<hex>",                        // per-file salt for the evidence hashes (not a secret)
  "n_decisions": 2,
  "disclaimer": "…",
  "records": [ /* one per scored (candidate, role) decision, in chain order */ ],
  "verification": { /* the issuer's own self-check, for convenience — re-derive it yourself */ }
}
```

Each **record**:

```jsonc
{
  "v": 1,
  "candidate_ref": "<sha256(salt + label)[:32]>",   // stable, de-identified candidate id
  "label": "Alice.txt",                              // human label (the operator's own, consented)
  "job": { "title", "employer", "required_skills":[ids], "preferred_skills":[ids],
           "must_have_skills":[ids], "min_education", "min_years" },
  "fit_score": 75.0,
  "grade": "B",
  "confidence": "high",
  "score_kind": "fit_readiness_not_hire_probability",
  "no_protected_attribute": true,                    // asserted: no protected attribute entered scoring
  "engine": "claude_cli",
  "ranker_formula": "fit = round( skills_subtotal x education x experience x must_have x integrity , 1 )",
  "explanation": {
    "subtotal": 75.0, "education_factor": 1.0, "experience_factor": 1.0,
    "must_have_factor": 1.0, "integrity_factor": 1.0, "final_score": 75.0,
    "components": [
      { "skill_id":"python", "bucket":"required", "status":"match",
        "points_possible":25.0, "points_earned":25.0,
        "evidence_sha256":"<sha256(salt + verbatim_quote)>" }   // PROOF a quote backed the point — NOT the quote
    ]
  },
  "prev_hash": null,                 // the previous record's record_hash (null for the first)
  "record_hash": "<hex>",            // sha256 of the canonical record WITHOUT record_hash + signature
  "signature": "<hex>"              // signature over record_hash
}
```

Evidence quotes are stored only as `sha256(salt + quote)` — the file is **de-identified by construction**
and never carries raw résumé text.

## Canonicalization

All hashing and signing operate on **canonical JSON bytes**:

- UTF-8, object keys **sorted lexicographically**, no insignificant whitespace
  (equivalent to `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
- **Numbers are normalized** so they survive a JSON round-trip in any language: an integer-valued number
  serializes with no decimal point or exponent (`75.0` → `75`), matching ECMAScript `JSON.stringify`.
  Implementations must apply this before hashing/signing, or a file re-serialized by a browser will not
  match one produced in Python.

## Verification algorithm

For the file, run all four checks. The file is **VERIFIED** only if all that apply pass.

1. **Reconciliation (reproducibility).** For each record, recompute
   `round(subtotal × education_factor × experience_factor × must_have_factor × integrity_factor, 1)`
   and confirm it equals `fit_score` (and `explanation.final_score`). Confirm the component
   `points_earned` sum to `subtotal` (allow ≤0.3 for cumulative rounding). → `all_reconcile`.
2. **Hash chain (tamper-evidence).** For each record, recompute `sha256(canonical(record − {record_hash,
   signature}))` and confirm it equals `record_hash`; confirm `prev_hash` equals the previous record's
   `record_hash` (and `null` for the first). Any edit to any record breaks this. → `chain_intact`.
3. **Signature.** For `ed25519`, verify each `signature` over `record_hash` using the file's
   `public_key`. (For `hmac-sha256`, verification requires the issuer's secret key — symmetric.) →
   `signatures_valid`.
4. **Issuer authenticity.** Obtain the issuer's public key **out-of-band** (e.g. `GET
   /api/defense-file/pubkey`, or a key published by the issuer) and confirm the file's `public_key`
   equals it. *Without this step a forger can sign with their own key and embed it* — step 3 alone only
   proves the file is self-consistent. → `issuer_verified`.

## Reference implementations

- Python library: `resume_matcher/audit/defense_file.py` — `verify_defense_file(file, expected_public_key=…)`.
- CLI: `python scripts/verify_defense_file.py defense-file.json --issuer-key <hex>` (exit 0 = verified).
- Web: `GET /verify` (public, no login) — drop a file, see the verdict.

The verifier needs nothing from the issuer except the file and the out-of-band public key. That is the
point: **verify, don't trust.**
