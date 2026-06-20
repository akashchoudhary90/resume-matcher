"""Streamlit coordinator dashboard (optional). Run: `streamlit run resume_matcher/ui/dashboard.py`
after installing requirements-extra.txt. Shows per-job shortlists, coaching, and the bias audit.

This is intentionally a thin view over the same pipeline the tests exercise; treat it as replaceable.
"""
from __future__ import annotations

from pathlib import Path

DATA = Path("data/synthetic")


def _run():  # pragma: no cover - requires streamlit + a browser
    import streamlit as st

    from ..ingestion.importer import import_students, load_jobs
    from ..inference.adapters.mock import MockAdapter
    from ..matching.pipeline import run_matching

    st.title("Resume Matcher — Coordinator Dashboard")
    st.caption("Scores are FIT/READINESS, not a probability of being hired.")

    if not (DATA / "students.csv").exists():
        st.warning("No data found. Run `python scripts/gen_synthetic.py` first.")
        return

    result = import_students(DATA / "students.csv", DATA / "resumes")
    jobs = load_jobs(DATA / "jobs.csv")
    st.info(result.summary())

    run = run_matching(result.candidates, jobs, adapter=MockAdapter(), shortlist_k=10)
    job_titles = {sl.job.job_id: f"{sl.job.title} @ {sl.job.employer}" for sl in run.shortlists}
    pick = st.selectbox("Job", list(job_titles), format_func=lambda j: job_titles[j])
    sl = next(s for s in run.shortlists if s.job.job_id == pick)
    for r in sl.ranked:
        st.write(f"**{r.candidate_id}** — fit {r.fit_score} (grade {r.grade}, conf {r.confidence.value})")
        if r.flags:
            st.caption("⚠ review flags: " + ", ".join(r.flags))


if __name__ == "__main__":  # pragma: no cover
    _run()
