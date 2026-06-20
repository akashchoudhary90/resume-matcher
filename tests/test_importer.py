from resume_matcher.ingestion.importer import import_students, load_jobs
from resume_matcher.ingestion.synthetic import generate_dataset


def test_import_stitches_and_reports_coverage(tmp_path):
    generate_dataset(tmp_path, n_students=30, n_jobs=6, seed=7)
    result = import_students(tmp_path / "students.csv", tmp_path / "resumes")

    assert len(result.candidates) == 30
    assert result.total_rows == 30
    # Some students intentionally have no public resume -> coverage < 100%, surfaced as a state.
    assert 0 < result.with_resume <= 30
    assert any(not c.has_resume for c in result.candidates)
    # Resume text was parsed into canonical skills and redacted (synthetic emails removed).
    with_resume = [c for c in result.candidates if c.has_resume]
    assert with_resume and all(c.skills for c in with_resume)
    assert all("@my.yorku.ca" not in c.text for c in with_resume)


def test_load_jobs(tmp_path):
    generate_dataset(tmp_path, n_students=10, n_jobs=5, seed=7)
    jobs = load_jobs(tmp_path / "jobs.csv")
    assert len(jobs) == 5
    assert all(j.required_skills for j in jobs)
