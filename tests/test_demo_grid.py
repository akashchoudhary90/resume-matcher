"""Multi-job fit grid: score the same résumés across several roles into a candidate×role matrix.

Core-deps tests exercise run_demo_grid directly; the HTTP test is skipped without FastAPI/httpx.
"""
import io

import pytest

from resume_matcher.api.demo import DemoError, SessionStore, run_demo_grid


def _files():
    return [
        ("Alice.txt", b"Senior Python developer. Strong SQL, built REST APIs and Docker pipelines. "
                      b"Bachelor of Science. 5 years experience. " * 3),
        ("Bob.txt", b"Data analyst. Excel and SQL reporting. Some Python. Diploma. " * 3),
    ]


def test_run_demo_grid_builds_matrix():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo_grid(store=store, files=_files(), jobs=[
        {"title": "Python Dev", "job_text": "Python developer with SQL and Docker."},
        {"title": "Data Analyst", "job_text": "Data analyst with SQL and Excel."},
    ])
    d = sess.to_dict()
    assert d["mode"] == "grid"
    grid = d["grid"]
    assert [j["title"] for j in grid["jobs"]] == ["Python Dev", "Data Analyst"]
    assert len(grid["candidates"]) == 2
    for c in grid["candidates"]:
        assert len(c["cells"]) == 2                      # one cell per role
        assert c["best_job_index"] in (0, 1)
        for cell in c["cells"]:                          # every cell keeps a full breakdown to drill in
            assert cell is None or cell["result"]["explanation"] is not None
    bests = [c["best_fit_score"] for c in grid["candidates"]]
    assert bests == sorted(bests, reverse=True)          # ranked by best fit across roles
    assert store.active_count() == 1                     # only the grid is kept (no per-role sessions)


def test_run_demo_grid_skips_role_with_no_skills():
    store = SessionStore(ttl_seconds=600)
    sess = run_demo_grid(store=store, files=_files(), jobs=[
        {"title": "Good", "job_text": "Python and SQL developer."},
        {"title": "Empty", "job_text": "We are a great company with free snacks and a fun culture."},
    ])
    grid = sess.to_dict()["grid"]
    assert len(grid["jobs"]) == 1                         # the skill-less role is dropped, not fatal
    assert any("skipped" in w.lower() for w in sess.warnings)


def test_run_demo_grid_too_many_roles():
    store = SessionStore()
    with pytest.raises(DemoError):
        run_demo_grid(store=store, files=_files(),
                      jobs=[{"job_text": f"Python and SQL role {i}."} for i in range(99)])


def test_run_demo_grid_requires_a_role():
    store = SessionStore()
    with pytest.raises(DemoError):
        run_demo_grid(store=store, files=_files(), jobs=[])


def test_grid_endpoint():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from resume_matcher.api.app import create_app

    client = TestClient(create_app())
    files = [("resumes", ("Alice.txt", io.BytesIO(b"Python SQL Docker developer. Bachelor. " * 4), "text/plain")),
             ("resumes", ("Bob.txt", io.BytesIO(b"Java developer. Some Python. " * 4), "text/plain"))]
    data = {
        "job_title": ["Python Dev", "Data Analyst"],
        "job_employer": ["Acme", "Acme"],
        "job_text": ["Python developer with SQL and Docker.", "Data analyst with SQL and Excel."],
    }
    r = client.post("/api/demo/run-grid", data=data, files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "grid"
    assert len(body["grid"]["jobs"]) == 2
    assert len(body["grid"]["candidates"]) == 2
    assert "alice@" not in r.text
