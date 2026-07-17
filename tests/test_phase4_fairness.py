"""Slices AH/AI: warm-intro fairness report (aggregate-only), the network-feature scoring guard,
and the boundary trio (graph never enters scoring / match_results; no audit import in the graph)."""
from __future__ import annotations

import pathlib

import pytest

from resume_matcher.audit.metrics import access_disparity
from resume_matcher.stores.data_planes import ProtectedDataError, ScoringStore


# ---- Slice AH: pure aggregate metric --------------------------------------------------------------
def test_access_disparity_four_fifths():
    # denominator = applicants per group; numerator = who received an intro
    denom = {"first_gen": 20, "not_first_gen": 20}
    fair = access_disparity({"first_gen": 8, "not_first_gen": 10}, denom)
    assert fair["four_fifths_pass"] is True and fair["min_impact_ratio"] == 0.8
    biased = access_disparity({"first_gen": 2, "not_first_gen": 12}, denom)
    assert biased["four_fifths_pass"] is False


def test_access_disparity_suppresses_small_denominator_and_reports_threshold():
    denom = {"big": 30, "tiny": 3}
    out = access_disparity({"big": 15}, denom)
    assert "tiny" not in out["rates"]                 # denominator below min_cell excluded entirely
    # a group with a denominator but a suppressed (None) numerator -> "below threshold", not 0
    denom2 = {"a": 10, "b": 10}
    out2 = access_disparity({"a": 6}, denom2)
    assert out2["rates"]["b"]["note"] == "below reporting threshold"
    # Phase 5 (A4): the suppressed group is BOUNDED, not unknown — b got at most 4/10 = 0.40, which
    # cannot reach 0.8 * 0.60. The old None ("<2 comparable groups") let suppression hide a provable
    # disparity behind an undecided verdict; it is now a FAIL wherever b's true rate sits.
    assert out2["rates"]["b"]["rate_bound_upper"] == 0.4
    assert out2["four_fifths_pass"] is False
    assert "suppressed group cannot exceed 0.8 threshold — human review required" in out2["notes"]


# ---- Slice AI: the network-feature scoring-plane guard (boundary #2 extended) ---------------------
def test_network_features_rejected_by_scoring_plane():
    for key in ("degree", "reachability", "intro_count", "connector_count", "network_poverty"):
        with pytest.raises(ProtectedDataError):
            ScoringStore.assert_no_protected({key: 5})
    # a legitimate scoring feature is still fine
    ScoringStore.assert_no_protected({"years_experience": 3})


# ---- the boundary trio: the graph never leaks into scoring ----------------------------------------
_SRC = pathlib.Path(__file__).resolve().parents[1] / "resume_matcher"


def test_pathfinder_and_graph_never_import_audit_or_scoring_planes():
    """The pathfinder/relationship modules must not import the audit plane or the ranker — the
    graph is a separate surface and cannot become a scoring backdoor."""
    for mod in ("stores/intros.py", "stores/relationships.py", "stores/graph.py"):
        text = (_SRC / mod).read_text(encoding="utf-8")
        assert "audit_store" not in text, f"{mod} must not import the audit plane"
        assert "from ..matching.ranker" not in text and "import ranker" not in text, \
            f"{mod} must not import the ranker/scoring path"


def test_match_results_schema_has_no_graph_columns():
    sql = (_SRC / "stores" / "migrations" / "001_platform.sql").read_text(encoding="utf-8")
    # find the match_results block and assert no intro/vouch/degree/connector column leaked in
    block = sql[sql.find("CREATE TABLE IF NOT EXISTS match_results"):]
    block = block[:block.find(");")].lower()
    for banned in ("intro", "vouch", "degree", "connector", "network"):
        assert banned not in block, f"match_results must not carry a graph column ({banned})"


def test_scoring_query_selects_only_redacted_text_not_graph():
    """The match job builds candidates from redacted_text only — never a graph join."""
    text = (_SRC / "api" / "platform.py").read_text(encoding="utf-8")
    # the candidate builder function must not reference graph_edges/intro tables
    start = text.find("def _candidate_from_row")
    end = text.find("def _match_posting_job")
    builder = text[start:end]
    assert "graph_edges" not in builder and "intro_requests" not in builder
