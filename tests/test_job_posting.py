"""Free-text job posting -> editable JobSpec."""
from resume_matcher.ingestion.job_posting import build_job_spec, detect_job_skills, parse_job_posting


def test_detect_skills_from_posting():
    text = "We want a Python engineer who knows SQL and Docker. React is a plus."
    detected = detect_job_skills(text)
    assert {"python", "sql", "docker", "react"} <= set(detected)


def test_parse_defaults_detected_skills_to_required_and_infers_education():
    text = "Junior analyst. Python and Excel needed. Bachelor degree required."
    spec, detected = parse_job_posting(text, title="Analyst", employer="Acme")
    assert spec.title == "Analyst" and spec.employer == "Acme"
    assert "python" in spec.required_skills and "excel" in spec.required_skills
    assert spec.preferred_skills == []
    assert spec.min_education == "bachelor"
    assert set(detected) == set(spec.required_skills)


def test_explicit_skill_buckets_respected_and_deduped():
    spec = build_job_spec(
        required_skills=["python", "sql"],
        preferred_skills=["sql", "react"],  # sql duplicated -> kept once, as required
    )
    assert spec.required_skills == ["python", "sql"]
    assert spec.preferred_skills == ["react"]


def test_empty_posting_yields_empty_skills():
    spec, detected = parse_job_posting("", title="Role")
    assert detected == []
    assert spec.required_skills == []
