"""Two-key fairness change-control: a scoring change that worsens the bias audit must fail CI.

The audit is over SYNTHETIC data and bias is never a scoring input — this gate just blocks a regression
in the four-fifths / exposure / homophily / proxy-leakage AUDIT metrics versus the committed baseline.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BASELINE = ROOT / "data" / "eval" / "fairness_baseline.json"


def _load_gate():
    spec = importlib.util.spec_from_file_location("eval_fairness", str(ROOT / "scripts" / "eval_fairness.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _baseline():
    return json.loads(_BASELINE.read_text(encoding="utf-8"))


def test_baseline_is_committed_and_sane():
    base = _baseline()
    floors = base["floors"]
    # four-fifths floors are capped at the 0.80 legal line (regression gate on already-low synthetic data)
    assert floors["min_race_min_impact_ratio"] <= 0.80
    assert floors["min_gender_min_impact_ratio"] <= 0.80


def test_check_passes_at_the_committed_baseline():
    gate = _load_gate()
    assert gate._check({"metrics": _baseline()["current"]}) == []


def test_check_detects_a_four_fifths_regression():
    gate = _load_gate()
    bad = dict(_baseline()["current"])
    bad["race_min_impact_ratio"] = 0.40                 # a scoring change tanks race impact ratio
    viol = gate._check({"metrics": bad})
    assert any("race_min_impact_ratio" in v for v in viol)


def test_check_detects_proxy_leakage_regression():
    gate = _load_gate()
    bad = dict(_baseline()["current"])
    bad["proxy_leakage_auc"] = 0.95                     # features now strongly predict the protected attr
    assert any("proxy_leakage_auc" in v for v in gate._check({"metrics": bad}))


def test_small_movement_within_tolerance_is_allowed():
    gate = _load_gate()
    base = _baseline()
    ok = dict(base["current"])
    ok["race_min_impact_ratio"] = base["floors"]["min_race_min_impact_ratio"] - 0.02  # within tol 0.03
    assert gate._check({"metrics": ok}) == []


def test_gate_runs_end_to_end_and_passes_on_current_code():
    pytest.importorskip("scipy")    # the audit needs scipy (fisher) + numpy
    env = {**os.environ, "RM_EMBEDDINGS": "tfidf", "RM_INFERENCE_BACKEND": "mock"}
    r = subprocess.run([sys.executable, "scripts/eval_fairness.py", "--check"],
                       capture_output=True, text=True, cwd=str(ROOT), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Fairness check OK" in r.stdout
