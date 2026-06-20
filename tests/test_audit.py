"""Bias-audit detection + data-plane separation (plan §D, boundary #2)."""
import numpy as np
import pytest

from resume_matcher.audit.metrics import homophily_disparity, selection_audit
from resume_matcher.audit.proxy_leakage import proxy_leakage
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


def test_homophily_disparity_is_the_reframed_hunch():
    labels = ["A"] * 12 + ["B"] * 12
    selected = [True] * 10 + [False] * 2 + [True] * 2 + [False] * 10
    out = homophily_disparity(labels, selected, reference_group="A", min_cell=5)
    assert out["computable"] and out["flagged"]
    assert out["disparity_ratio"] < 0.8


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
