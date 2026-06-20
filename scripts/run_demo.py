"""End-to-end demo on synthetic data: ingest -> match -> rank -> coach -> bias audit.

Runs entirely on the core dependencies with the deterministic Mock adapter (no LLM backend needed).
Run: python scripts/run_demo.py
"""
from __future__ import annotations

import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from resume_matcher.audit.metrics import selection_audit  # noqa: E402
from resume_matcher.audit.proxy_leakage import proxy_leakage  # noqa: E402
from resume_matcher.ingestion.importer import import_students, load_jobs  # noqa: E402
from resume_matcher.ingestion.synthetic import generate_dataset  # noqa: E402
from resume_matcher.inference.adapters.mock import MockAdapter  # noqa: E402
from resume_matcher.matching.pipeline import run_matching  # noqa: E402
from resume_matcher.stores.data_planes import AuditStore  # noqa: E402

DATA = pathlib.Path("data/synthetic")


def _ensure_data() -> None:
    if not (DATA / "students.csv").exists():
        print("No synthetic data found — generating...")
        generate_dataset(DATA, n_students=60, n_jobs=12, seed=42)


def _load_audit_store(candidate_ids: list[str]) -> tuple[AuditStore, list[str | None]]:
    store = AuditStore()
    path = DATA / "self_id.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                store.record_self_id(
                    row["candidate_id"],
                    {"race_ethnicity": row["race_ethnicity"], "gender": row["gender"]},
                )
    return store, store.labels_for(candidate_ids, "race_ethnicity")


def main() -> None:
    _ensure_data()

    # 1) Ingest (PII redaction happens inside the importer/parser).
    imported = import_students(DATA / "students.csv", DATA / "resumes")
    jobs = load_jobs(DATA / "jobs.csv")
    print("=== Ingestion ===")
    print(" ", imported.summary())

    # 2) Match -> rank -> coach (deterministic Mock adapter; no model required).
    run = run_matching(imported.candidates, jobs, adapter=MockAdapter(), retrieve_k=30, shortlist_k=8)

    sl = run.shortlists[0]
    print(f"\n=== Top candidates for {sl.job.title} @ {sl.job.employer} ({sl.job.job_id}) ===")
    print("  (fit/readiness score — NOT a probability of being hired)")
    for r in sl.ranked[:5]:
        flags = f"  flags={r.flags}" if r.flags else ""
        print(f"  {r.candidate_id}  fit={r.fit_score:>5}  grade={r.grade}  conf={r.confidence.value}{flags}")

    # 3) Coaching for the top candidate.
    coached = sl.coaching[0] if sl.coaching else None
    if coached:
        print(f"\n=== Coaching for {sl.ranked[0].candidate_id} on {coached['title']} ===")
        for g in coached["blocking_gaps"]:
            print(f"  blocking: {g['skill']} ({g['difficulty']}) -> {g['action']}")
        if not coached["blocking_gaps"]:
            print("  no essential gaps — strong fit.")

    # 4) Closest-fit roles for one candidate.
    any_cid = sl.ranked[0].candidate_id
    cf = run.closest_fit.get(any_cid)
    if cf:
        print(f"\n=== Roles {any_cid} is closest to ===")
        for row in cf.ranked:
            print(f"  {row['title']} @ {row['employer']}: fit={row['fit_score']} blocking={row['blocking_gaps']}")

    # 5) Bias audit (aggregate-only; protected labels never touched scoring).
    pool_ids = [c.candidate_id for c in imported.candidates]
    _store, labels = _load_audit_store(pool_ids)
    selected = set().union(*[set(s.selected_ids) for s in run.shortlists]) if run.shortlists else set()
    selected_mask = [cid in selected for cid in pool_ids]
    report = selection_audit(labels, selected_mask, attribute="race_ethnicity", min_cell=5)
    print("\n=== Bias audit (race_ethnicity) — four-fifths impact ratio + Fisher's exact ===")
    for g in report.groups:
        ir = "n/a" if g.impact_ratio is None else g.impact_ratio
        print(f"  {g.group:>8}: n={g.n:>3} selected={g.selected:>3} rate={g.selection_rate:.2f} "
              f"impact_ratio={ir} fisher_p={g.fisher_p}")
    print(f"  four-fifths pass: {report.four_fifths_pass}  (min impact ratio={report.min_impact_ratio})")
    for note in report.notes:
        print(f"  note: {note}")

    # 6) Proxy-leakage diagnostic: do the scoring features leak the protected attribute?
    feats = np.array(
        [[len(c.skills), c.years_experience, len(c.text)] for c in imported.candidates], dtype=float
    )
    leak = proxy_leakage(feats, labels, target_group="Group A")
    print("\n=== Proxy-leakage diagnostic (features -> 'Group A') ===")
    print(f"  {leak}")
    print("\nDone. This ran on the deterministic Mock backend — set RM_INFERENCE_BACKEND to swap.")


if __name__ == "__main__":
    main()
