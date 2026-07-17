"""Bias-audit detection + data-plane separation (plan §D, boundary #2)."""
import numpy as np
import pytest

from resume_matcher.audit.metrics import (
    access_disparity,
    exposure_parity,
    homophily_disparity,
    origin_impact,
    selection_audit,
)
from resume_matcher.audit.proxy_leakage import _auc, proxy_leakage
from resume_matcher.stores.data_planes import AuditStore, ProtectedDataError, ScoringStore


def test_selection_audit_flags_seeded_disparity():
    labels = ["A"] * 12 + ["B"] * 12
    selected = [True] * 10 + [False] * 2 + [True] * 2 + [False] * 10  # A: 10/12, B: 2/12
    report = selection_audit(labels, selected, attribute="race_ethnicity", min_cell=5)
    assert report.four_fifths_pass is False
    assert report.min_impact_ratio is not None and report.min_impact_ratio < 0.8
    assert report.flagged()


def test_selection_audit_passes_when_balanced():
    labels = ["A"] * 10 + ["B"] * 10
    selected = [True, False] * 10  # 5/10 each
    report = selection_audit(labels, selected, min_cell=5)
    assert report.four_fifths_pass is True


def test_selection_audit_single_group_cannot_pass():
    # A4: one group is a ratio against itself. The old code scored impact_ratio 1.0 and PASSED —
    # an audit that only ever saw one group reported "no disparate impact" forever.
    report = selection_audit(["A"] * 10, [True] * 5 + [False] * 5, min_cell=5)
    assert report.four_fifths_pass is None and report.min_impact_ratio is None
    assert "fewer than 2 comparable groups" in report.notes
    # a group below min_cell is not comparable either, so it can't be the second group
    small = selection_audit(["A"] * 10 + ["B"] * 3, [True] * 5 + [False] * 5 + [False] * 3, min_cell=5)
    assert small.four_fifths_pass is None


def test_selection_audit_zero_selections_is_not_a_pass():
    # A4: nobody selected => no reference rate. The `or 1.0` fallback divided by an invented 1.0 and
    # returned ratios of 0.0/1.0 for every group; a run before any shortlist exists is undecided.
    report = selection_audit(["A"] * 10 + ["B"] * 10, [False] * 20, min_cell=5)
    assert report.four_fifths_pass is None
    assert "no selections yet — audit not meaningful" in report.notes


def test_access_disparity_bounded_suppression_matrix():
    # A4: the three reachable verdicts when a numerator cell is suppressed.
    # FAIL — b's ceiling (4/10=0.40) is under 0.8 * 0.60; true rate is irrelevant.
    fail = access_disparity({"a": 6}, {"a": 10, "b": 10})
    assert fail["four_fifths_pass"] is False

    # INDETERMINATE — b's ceiling (4/100=0.04) clears 0.8 * 0.02, but its floor is 0: unprovable
    # either way, so a human looks at un-suppressed counts. Never a silent pass.
    straddle = access_disparity({"a": 2}, {"a": 100, "b": 100})
    assert straddle["four_fifths_pass"] is None
    assert "indeterminate under suppression — human review required" in straddle["notes"]

    # PASS — only reachable with no suppressed group at all.
    clean = access_disparity({"a": 5, "b": 5}, {"a": 10, "b": 10})
    assert clean["four_fifths_pass"] is True and clean["notes"] == []


def test_access_disparity_zero_access_cohort_is_undecided():
    # A4: no group got an intro -> no reference rate (the removed `or 1.0` used to invent one).
    out = access_disparity({"a": 0, "b": 0}, {"a": 10, "b": 10})
    assert out["four_fifths_pass"] is None and out["min_impact_ratio"] is None
    assert "no access in any comparable group — audit not meaningful" in out["notes"]


def test_origin_impact_ratios_and_threshold():
    # C2: does a bridged intro convert like an organic one?
    counts = {"organic": {"requested": 100, "accepted": 50, "shortlisted": 20, "hired": 10},
              "bridged": {"requested": 50, "accepted": 30, "shortlisted": 10, "hired": 2}}
    out = origin_impact(counts)
    assert out["by_origin"]["bridged"]["stages"]["accepted"]["rate"] == 0.6
    assert out["bridged_over_organic"]["accepted"]["ratio"] == 1.2      # 0.6 / 0.5
    # 2 hires is below min_cell: the ratio is withheld rather than published as noise
    assert out["bridged_over_organic"]["hired"]["ratio"] is None
    assert out["bridged_over_organic"]["hired"]["note"] == "below threshold"
    # a tiny bridged cohort yields no rates at all
    thin = origin_impact({"organic": {"requested": 100, "accepted": 50},
                          "bridged": {"requested": 3, "accepted": 3}})
    assert thin["by_origin"]["bridged"]["stages"]["accepted"]["rate"] is None
    assert thin["bridged_over_organic"]["accepted"]["ratio"] is None


def test_origin_impact_suppresses_the_counts_not_just_the_ratios():
    """REGRESSION: the report used to null the RATE and publish the raw `n`/`requested` under it —
    '3 bridged intros, 1 hired' names a student once the coordinator holds the roster."""
    out = origin_impact({"organic": {"requested": 100, "accepted": 50, "shortlisted": 20, "hired": 10},
                         "bridged": {"requested": 3, "accepted": 3, "shortlisted": 1, "hired": 1}})
    bridged = out["by_origin"]["bridged"]
    assert bridged["requested"] is None            # the cohort itself is the small cell
    assert all(cell["n"] is None and cell["rate"] is None for cell in bridged["stages"].values())
    assert 3 not in _counts_in(bridged) and 1 not in _counts_in(bridged)

    # complementary suppression: an exact denominator beside a hidden stage narrows it, so the
    # denominator is banded — and the visible stages keep their rates.
    banded = origin_impact({"organic": {"requested": 52, "accepted": 50, "shortlisted": 20,
                                        "hired": 2},
                            "bridged": {"requested": 50, "accepted": 30}})
    organic = banded["by_origin"]["organic"]
    assert organic["requested"] == "50-55"
    assert organic["stages"]["hired"]["n"] is None
    assert organic["stages"]["hired"]["rate"] is None   # else n == round(rate * requested)
    assert organic["stages"]["accepted"]["n"] == 50


def _counts_in(cell: dict) -> list:
    """Every scalar a caller could read off one origin block — nothing sub-min_cell may be in here."""
    vals = [cell["requested"]]
    for stage in cell["stages"].values():
        vals += [stage["n"], stage["rate"]]
    return vals


def test_homophily_disparity_is_the_reframed_hunch():
    labels = ["A"] * 12 + ["B"] * 12
    selected = [True] * 10 + [False] * 2 + [True] * 2 + [False] * 10
    out = homophily_disparity(labels, selected, reference_group="A", min_cell=5)
    assert out["computable"] and out["flagged"]
    assert out["disparity_ratio"] < 0.8


def test_exposure_parity_rank_aware():
    # #26: rank-aware exposure (was dead code, now wired into audit()). Group ranked lower is under-exposed.
    labels = ["A"] * 6 + ["B"] * 6
    ranks = list(range(1, 7)) + list(range(7, 13))  # A ranked 1-6, B ranked 7-12
    out = exposure_parity(labels, ranks, min_cell=5)
    assert out["exposure"]["A"] > out["exposure"]["B"]
    assert out["parity_ratio"] is not None and out["parity_ratio"] < 1.0


def test_proxy_leakage_detects_correlated_features_and_not_independent():
    rng = np.random.default_rng(0)
    labels = ["A"] * 30 + ["B"] * 30
    # Correlated: group A clusters high on feature 0.
    corr = np.vstack([rng.normal(5, 1, (30, 2)), rng.normal(0, 1, (30, 2))])
    leaked = proxy_leakage(corr, labels, target_group="A")
    assert leaked["computable"] and leaked["leakage"]

    # Independent: features carry no information about the label.
    indep = rng.normal(0, 1, (60, 2))
    clean = proxy_leakage(indep, labels, target_group="A")
    assert clean["computable"] and clean["auc"] < 0.7
    # A12: which estimator produced the number is reported, not swallowed by a blanket except.
    assert clean["method"] in ("logreg_cv", "fallback_linear")


def test_auc_uses_midranks_for_ties():
    # A12: a constant-output classifier knows nothing -> AUC 0.5. Ordinal ranking gave every tie to
    # whichever class was concatenated first (positives), scoring a no-information model at 1.0.
    y = np.array([1, 1, 0, 0])
    assert _auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    # a partial tie splits only the tied pairs: pos {1.0, 0.5} vs neg {0.5, 0.0} -> 3.5/4
    assert _auc(y, np.array([1.0, 0.5, 0.5, 0.0])) == 0.875
    assert _auc(y, np.array([1.0, 0.9, 0.2, 0.1])) == 1.0        # no ties, perfect separation


def test_proxy_leakage_fallback_keeps_its_intercept():
    # A12: the fallback standardizes features THEN appends the ones column. Appending first meant
    # (1 - 1)/sd == 0 — the intercept vanished, and a lopsided cohort (base rate far from 0.5) could
    # not be fit at all, understating leakage on exactly the skewed groups the audit cares about.
    rng = np.random.default_rng(1)
    labels = ["A"] * 10 + ["B"] * 50                  # heavily skewed base rate
    feats = np.vstack([rng.normal(4, 0.5, (10, 2)), rng.normal(0, 0.5, (50, 2))])
    out = proxy_leakage(feats, labels, target_group="A")
    assert out["computable"] and out["leakage"] and out["auc"] > 0.9


def test_scoring_store_rejects_protected_attributes():
    store = ScoringStore()
    with pytest.raises(ProtectedDataError):
        store.assert_no_protected({"python": 1, "ethnicity": "x"})


def test_audit_store_rejects_non_auditable_keys_and_is_aggregate_only():
    audit = AuditStore()
    audit.record_self_id("S1", {"race_ethnicity": "A", "gender": "woman"})
    with pytest.raises(ProtectedDataError):
        audit.record_self_id("S2", {"home_address": "secret"})
    # The only egress is an aligned label list for aggregate analysis.
    assert audit.labels_for(["S1", "missing"], "race_ethnicity") == ["A", None]
