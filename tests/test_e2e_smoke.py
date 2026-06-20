"""End-to-end smoke test: synthetic batch -> ingest -> match -> rank -> coach, on the Mock backend."""
from resume_matcher.ingestion.importer import import_students, load_jobs
from resume_matcher.ingestion.synthetic import generate_dataset
from resume_matcher.inference.adapters.mock import MockAdapter
from resume_matcher.matching.pipeline import run_matching


def test_pipeline_runs_and_scores_are_honest(tmp_path):
    generate_dataset(tmp_path, n_students=30, n_jobs=6, seed=11)
    imported = import_students(tmp_path / "students.csv", tmp_path / "resumes")
    jobs = load_jobs(tmp_path / "jobs.csv")

    run = run_matching(imported.candidates, jobs, adapter=MockAdapter(), retrieve_k=20, shortlist_k=8)

    assert len(run.shortlists) == len(jobs)
    assert any(sl.ranked for sl in run.shortlists)

    for sl in run.shortlists:
        # Shortlist is sorted by fit, scores are in range, and labeled honestly (not a hire %).
        fits = [r.fit_score for r in sl.ranked]
        assert fits == sorted(fits, reverse=True)
        for r in sl.ranked:
            assert 0.0 <= r.fit_score <= 100.0
            assert r.grade in {"A", "B", "C", "D"}
            assert r.score_kind == "fit_readiness_not_hire_probability"
        assert len(sl.coaching) == len(sl.ranked)

    # Every scored candidate gets a closest-fit summary.
    assert run.closest_fit
    for cf in run.closest_fit.values():
        assert cf.ranked
