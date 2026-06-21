"""Accuracy measurement harness — runs the scorer on labeled examples and reports agreement."""
from resume_matcher.matching.benchmark import fit_to_label, load_examples, run_benchmark


def test_fit_to_label_bands():
    assert fit_to_label(90) == "strong"
    assert fit_to_label(55) == "ok"
    assert fit_to_label(20) == "weak"


def test_benchmark_runs_on_seed_and_reports_metrics():
    out = run_benchmark(load_examples())
    m = out["metrics"]
    assert m["n"] >= 9 and m["n_labeled"] >= 9
    # The seed is deliberately clear-cut, so the deterministic scorer should agree strongly.
    assert m["label_accuracy"] >= 0.7
    assert m["within_one_bucket"] == 1.0
    assert m["spearman"] is not None and m["spearman"] > 0.7
    assert m["mae"] is not None
    # every row carries both the tool verdict and the human label for inspection
    assert all("tool_fit" in r and "human_label" in r for r in out["rows"])
