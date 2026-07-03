"""Variance-aware benchmarking + per-backend baselines (the harness for measuring the REAL engine).

The real Claude engine is non-deterministic, so the benchmark runs N times and reports the spread.
These tests prove the aggregation, the baseline emit/merge, and the gate's skip-on-missing-baseline
WITHOUT needing the model: the mock is deterministic (stdev 0), and a tiny alternating stub
manufactures known run-to-run variance.
"""
import importlib.util
import json
import pathlib

import pytest

from resume_matcher.inference.adapter import InferenceAdapter
from resume_matcher.inference.schema import MatchExtraction, MatchStatus, SkillEvidence
from resume_matcher.matching.benchmark import (
    _agg,
    load_examples,
    resolve_dataset,
    run_benchmark_repeated,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script():
    """Import scripts/eval_accuracy.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "eval_accuracy_mod", _ROOT / "scripts" / "eval_accuracy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- _agg -------------------------------------------------------------------------------------

def test_agg_basic_and_edges():
    assert _agg([None, None]) is None                 # metric undefined every run
    one = _agg([0.5])
    assert one == {"mean": 0.5, "stdev": 0.0, "min": 0.5, "max": 0.5, "n": 1}
    a = _agg([0.2, 0.4, 0.6])
    assert a["mean"] == 0.4 and a["min"] == 0.2 and a["max"] == 0.6 and a["stdev"] > 0
    assert _agg([0.3, None, 0.5])["n"] == 2           # None values are dropped, not counted


# ---- run_benchmark_repeated -------------------------------------------------------------------

def test_repeated_deterministic_backend_has_zero_spread():
    ex = load_examples(resolve_dataset("coordinator"))
    rep = run_benchmark_repeated(ex, adapter=None, runs=3)  # None -> mock (deterministic)
    assert rep["runs"] == 3 and rep["n"] >= 20
    for k in ("label_accuracy", "within_one_bucket", "spearman", "mae"):
        assert rep["aggregate"][k]["stdev"] == 0.0      # mock never wobbles
    # per-example fit is also perfectly stable
    assert all(v["stdev"] == 0.0 for v in rep["per_example_fit"].values())


class _AlternatingAdapter(InferenceAdapter):
    """Marks the first required skill match / missing on alternating calls, so with an ODD number of
    examples each example's verdict flips every run -> guaranteed metric + per-example spread."""

    name = "stub"
    is_local = True

    def __init__(self):
        self.calls = 0

    def _extract(self, candidate, job):
        match = self.calls % 2 == 0
        self.calls += 1
        sid = (job.required_skills or ["python"])[0]
        ev = [SkillEvidence(
            skill_id=sid, skill_name=sid,
            status=MatchStatus.match if match else MatchStatus.missing,
            evidence_span="python" if match else None)]
        return MatchExtraction(candidate_id=candidate.candidate_id, job_id=job.job_id,
                               skill_matches=ev)


def _stub_examples():
    scores = [60, 80, 95]  # varied so spearman is defined (constant human scores -> nan)
    return [
        {"id": f"E{i}", "job": {"title": "Dev", "required_skills": ["python"]},
         "resume_text": f"Candidate {i} writes python every day and ships code.",
         "human": {"label": "strong", "score": scores[i]}}
        for i in range(3)  # ODD count -> per-example verdict flips each run
    ]


def test_repeated_nondeterministic_backend_shows_spread():
    rep = run_benchmark_repeated(_stub_examples(), adapter=_AlternatingAdapter(), runs=3)
    agg = rep["aggregate"]
    # At least one aggregate metric wobbles, and at least one example's fit has min < max.
    assert any(agg[k] and agg[k]["stdev"] > 0 for k in ("label_accuracy", "mae"))
    assert any(v["min"] < v["max"] for v in rep["per_example_fit"].values())


# ---- baseline emit / write / check ------------------------------------------------------------

def test_suggested_baseline_floors_sit_below_measured_mean():
    mod = _load_script()
    out = mod._evaluate("coordinator", "mock", runs=2)
    b = mod._suggest_baseline(out)
    assert b["min_label_accuracy"] <= out["metrics"]["label_accuracy"]
    assert b["max_mae"] >= out["metrics"]["mae"]
    assert "measured" in b and b["measured"]["label_accuracy"]["n"] == 2


def test_check_skips_backend_without_baseline_but_passes_mock():
    mod = _load_script()
    # mock has a committed baseline -> real (empty = passing) violation list
    assert mod._check(mod._evaluate("coordinator", "mock", runs=1)) == []
    # a backend with no committed baseline -> None (caller treats as SKIP, never a failure)
    fake = {"dataset": "coordinator", "backend": "some_new_engine", "metrics": {}}
    assert mod._check(fake) is None


def test_baseline_for_tolerates_legacy_flat_schema(tmp_path, monkeypatch):
    mod = _load_script()
    legacy = {"datasets": {"coordinator": {"backend": "mock", "min_label_accuracy": 0.2}}}
    p = tmp_path / "b.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(mod, "_BASELINE", p)
    assert mod._baseline_for({"dataset": "coordinator", "backend": "mock"})["min_label_accuracy"] == 0.2
    assert mod._baseline_for({"dataset": "coordinator", "backend": "claude_cli"}) is None


def test_write_baseline_merges_without_clobbering_mock(tmp_path, monkeypatch):
    mod = _load_script()
    # Start from a copy of the real per-backend baseline file.
    real = json.loads((_ROOT / "data" / "eval" / "baseline_metrics.json").read_text(encoding="utf-8"))
    p = tmp_path / "b.json"
    p.write_text(json.dumps(real), encoding="utf-8")
    monkeypatch.setattr(mod, "_BASELINE", p)

    out = mod._evaluate("coordinator", "mock", runs=1)
    out["backend"] = "claude_cli"  # pretend these are opus numbers
    mod._write_baseline(out)

    written = json.loads(p.read_text(encoding="utf-8"))
    ds = written["datasets"]["coordinator"]
    assert "mock" in ds and "claude_cli" in ds                     # both present, mock intact
    assert "min_label_accuracy" in ds["claude_cli"]


def test_assert_backend_available_exits_without_token(monkeypatch):
    mod = _load_script()
    from resume_matcher.inference.adapters import claude_cli as cc
    monkeypatch.setattr(cc, "available", lambda: False)
    with pytest.raises(SystemExit):
        mod._assert_backend_available("claude_cli")
    mod._assert_backend_available("mock")  # non-claude backends never gate on the token


# ---- review fixes -----------------------------------------------------------------------------

def test_collapsed_metric_is_flagged_not_reported_as_stable():
    # A metric non-finite on some runs (nan) must NOT masquerade as zero-variance stable: n < runs
    # is surfaced in the report line.
    mod = _load_script()
    partial = _agg([0.6, float("nan"), float("nan")])
    assert partial["n"] == 1                              # 2 collapsed runs dropped
    line = mod._fmt_metric(partial, runs=3)
    assert "1/3" in line and "!!" in line                # the report warns instead of hiding it
    full = mod._fmt_metric(_agg([0.6, 0.6, 0.6]), runs=3)
    assert "!!" not in full                               # a genuinely stable metric is not flagged


def test_check_gates_worst_run_not_mean(tmp_path, monkeypatch):
    # A floor is a hard minimum: a run below it is a regression even if the MEAN clears.
    mod = _load_script()
    # mean 0.5 meets the floor, but the worst run (min 0.4) does not.
    out = {"dataset": "coordinator", "backend": "claude_cli",
           "aggregate": {"label_accuracy": {"mean": 0.5, "stdev": 0.1, "min": 0.4, "max": 0.6, "n": 2},
                         "within_one_bucket": None, "spearman": None, "mae": None}}
    p = tmp_path / "b.json"
    p.write_text(json.dumps(
        {"datasets": {"coordinator": {"claude_cli": {"min_label_accuracy": 0.5}}}}), encoding="utf-8")
    monkeypatch.setattr(mod, "_BASELINE", p)
    viol = mod._check(out)
    assert viol and any("worst run" in v for v in viol)   # caught despite mean == floor


def test_suggested_baseline_role_and_spearman_clamp():
    mod = _load_script()
    # mock numbers are never labeled a "real-engine" floor
    mock_role = mod._suggest_baseline(mod._evaluate("coordinator", "mock", runs=1))["role"]
    assert "real-engine" not in mock_role and "mock" in mock_role.lower()
    # a poorly-ranking engine's spearman floor is clamped to the mathematical minimum, so the gate
    # stays triggerable (a floor below -1 could never fire)
    out = {"backend": "claude_cli", "runs": 3,
           "aggregate": {"label_accuracy": None, "within_one_bucket": None,
                         "spearman": {"mean": -0.8, "stdev": 0.3, "min": -1.0, "max": -0.5, "n": 3},
                         "mae": None}}
    assert mod._suggest_baseline(out)["min_spearman"] == -1.0
