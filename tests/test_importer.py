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


def test_safe_years_parses_freeform():
    from resume_matcher.ingestion.importer import _safe_years

    assert _safe_years("3 years") == 3.0
    assert _safe_years("5+") == 5.0
    assert _safe_years("1-2") == 1.0
    assert _safe_years("4") == 4.0
    assert _safe_years("N/A") is None
    assert _safe_years("   ") is None
    assert _safe_years(None) is None


def test_import_survives_freeform_years_cells(tmp_path):
    # Real Handshake exports carry free-form years; one bad cell must NOT abort the whole import.
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "candidate_id,email,name,education_level,years_experience\n"
        "S1,a@x.com,Alice,bachelor,3 years\n"
        "S2,b@x.com,Bob,master,5+\n"
        "S3,c@x.com,Carol,bachelor,N/A\n"
        "S4,d@x.com,Dave,bachelor,\n",
        encoding="utf-8",
    )
    result = import_students(csv_path)  # no resume_dir -> no crash, all rows kept
    assert result.total_rows == 4 and len(result.candidates) == 4


def test_load_jobs(tmp_path):
    generate_dataset(tmp_path, n_students=10, n_jobs=5, seed=7)
    jobs = load_jobs(tmp_path / "jobs.csv")
    assert len(jobs) == 5
    assert all(j.required_skills for j in jobs)
