"""Lint for the eval datasets — the ground truth the accuracy gate trusts.

As the coordinator set grows (multi-judge synthetic examples appended over time), these invariants
keep it usable: unique ids, complete schema, and label/score-band consistency (strong >= 75,
ok 50-74, weak < 50 — the empirical bands of the original 24 and the rubric given to judge panels).
"""
from resume_matcher.matching.benchmark import (
    example_stratum,
    load_examples,
    resolve_dataset,
    stratum_breakdown,
)

_BANDS = {"strong": (75, 100), "ok": (50, 74.999), "weak": (0, 49.999)}


def _coordinator():
    return load_examples(resolve_dataset("coordinator"))


def test_coordinator_ids_unique_and_wellformed():
    exs = _coordinator()
    ids = [e.get("id") for e in exs]
    assert len(ids) == len(set(ids)), "duplicate example ids"
    for e in exs:
        assert e.get("id") and e.get("resume_text") and len(e["resume_text"]) >= 50, e.get("id")
        job = e.get("job", {})
        assert job.get("title"), e["id"]
        assert job.get("required_skills"), e["id"]
        must = set(job.get("must_have_skills") or [])
        assert must <= set(job["required_skills"]), f"{e['id']}: must-haves not subset of required"


def test_coordinator_labels_consistent_with_scores():
    for e in _coordinator():
        h = e.get("human", {})
        label, score = h.get("label"), h.get("score")
        assert label in _BANDS, f"{e['id']}: bad label {label!r}"
        assert isinstance(score, (int, float)) and 0 <= score <= 100, f"{e['id']}: bad score {score!r}"
        lo, hi = _BANDS[label]
        assert lo <= score <= hi, f"{e['id']}: label {label} inconsistent with score {score}"


def test_stratum_assignment_and_breakdown():
    exs = _coordinator()
    strata = {example_stratum(e) for e in exs}
    assert "unknown" not in strata
    # The breakdown covers every example exactly once.
    rows = [{"stratum": example_stratum(e), "tool_fit": 50.0, "tool_label": "ok",
             "human_label": e["human"]["label"], "human_score": e["human"]["score"]} for e in exs]
    br = stratum_breakdown(rows)
    assert sum(s["n"] for s in br) == len(exs)
    assert all(s["mae"] is not None for s in br)
