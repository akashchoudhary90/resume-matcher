"""JD-file upload + bulk folder×folder matching (a folder of JDs scored against a folder of résumés)."""
import io

import pytest

from resume_matcher.api.demo import (
    MAX_JOBS,
    DemoError,
    SessionStore,
    extract_job_text,
    run_demo_grid_from_files,
)


def test_extract_job_text_reads_txt():
    assert "Python" in extract_job_text("jd.txt", b"We need Python and SQL.")


def test_grid_from_files_builds_matrix():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo_grid_from_files(
        store=store,
        jd_files=[("Python Dev.txt", b"Python developer with SQL and Docker."),
                  ("Data Analyst.txt", b"Data analyst with SQL and Excel.")],
        resume_files=[("Alice.txt", b"Senior Python developer. SQL, Docker, REST. Bachelor. 5 years. " * 3),
                      ("Bob.txt", b"Data analyst. Excel and SQL reporting. Diploma. " * 3)],
    )
    grid = sess.to_dict()["grid"]
    assert [j["title"] for j in grid["jobs"]] == ["Python Dev", "Data Analyst"]   # titles from filenames
    assert len(grid["candidates"]) == 2
    for c in grid["candidates"]:
        assert len(c["cells"]) == 2 and c["best_job_index"] in (0, 1)


def test_grid_from_files_caps_with_honest_message():
    store = SessionStore(ttl_seconds=600)
    jds = [(f"role{i}.txt", b"Python and SQL developer.") for i in range(MAX_JOBS + 2)]
    sess = run_demo_grid_from_files(
        store=store, jd_files=jds,
        resume_files=[("Alice.txt", b"Python and SQL developer. " * 4)],
    )
    assert len(sess.to_dict()["grid"]["jobs"]) == MAX_JOBS                          # capped
    assert any("Demo limit" in w and "upgrade" in w.lower() for w in sess.warnings)  # honest, not silent


def test_grid_from_files_requires_both_sides():
    store = SessionStore()
    with pytest.raises(DemoError):
        run_demo_grid_from_files(store=store, jd_files=[], resume_files=[("a.txt", b"python")])
    with pytest.raises(DemoError):
        run_demo_grid_from_files(store=store, jd_files=[("a.txt", b"python sql")], resume_files=[])


def _api_client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app
    return TestClient(create_app())


def test_parse_job_file_endpoint():
    client = _api_client()
    r = client.post(
        "/api/demo/parse-job-file",
        files=[("jd", ("Backend Dev.txt",
                       io.BytesIO(b"Python developer with SQL and Docker. Bachelor required."),
                       "text/plain"))],
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert "Python" in d["text"]
    assert {"python", "sql"} <= {s["id"] for s in d["detected_skills"]}
    assert d["title"] == "Backend Dev"


def test_run_grid_files_endpoint():
    client = _api_client()
    files = [
        ("jds", ("Python Dev.txt", io.BytesIO(b"Python developer with SQL and Docker."), "text/plain")),
        ("jds", ("Analyst.txt", io.BytesIO(b"Data analyst with SQL and Excel."), "text/plain")),
        ("resumes", ("Alice.txt", io.BytesIO(b"Python SQL Docker developer. Bachelor. " * 4), "text/plain")),
        ("resumes", ("Bob.txt", io.BytesIO(b"Excel and SQL analyst. Diploma. " * 4), "text/plain")),
    ]
    r = client.post("/api/demo/run-grid-files", files=files)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "grid"
    assert len(d["grid"]["jobs"]) == 2
    assert len(d["grid"]["candidates"]) == 2
