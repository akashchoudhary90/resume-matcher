"""Bias-audit metrics — the NYC Local Law 144 template plus rank-aware and homophily checks.

All functions take aligned lists (one entry per candidate). Protected labels come from the
AuditStore's aggregate egress. A group below `min_cell` is excluded from ratios and flagged, because
the four-fifths rule is statistically weak on tiny cells (we confirm with Fisher's exact test).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import fisher_exact


@dataclass
class GroupStat:
    group: str
    n: int
    selected: int
    selection_rate: float
    impact_ratio: float | None = None  # vs the most-selected group
    fisher_p: float | None = None
    below_min_cell: bool = False


@dataclass
class AuditReport:
    attribute: str
    groups: list[GroupStat] = field(default_factory=list)
    min_impact_ratio: float | None = None
    four_fifths_pass: bool | None = None
    notes: list[str] = field(default_factory=list)

    def flagged(self) -> bool:
        return self.four_fifths_pass is False


def _clean(labels: list[str | None], selected_mask: list[bool]) -> tuple[list[str], np.ndarray]:
    ls, ss = [], []
    for lab, sel in zip(labels, selected_mask):
        if lab is not None:
            ls.append(lab)
            ss.append(bool(sel))
    return ls, np.array(ss, dtype=bool)


def selection_audit(
    labels: list[str | None],
    selected_mask: list[bool],
    attribute: str = "race_ethnicity",
    min_cell: int = 5,
) -> AuditReport:
    """Four-fifths impact-ratio audit with Fisher's exact significance per group."""
    labs, sel = _clean(labels, selected_mask)
    report = AuditReport(attribute=attribute)
    if not labs:
        report.notes.append("no self-identified candidates; audit not computable")
        return report

    groups = sorted(set(labs))
    labs_arr = np.array(labs)
    stats: dict[str, GroupStat] = {}
    for g in groups:
        mask = labs_arr == g
        n = int(mask.sum())
        s = int(sel[mask].sum())
        rate = s / n if n else 0.0
        stats[g] = GroupStat(group=g, n=n, selected=s, selection_rate=rate, below_min_cell=n < min_cell)

    eligible = [s for s in stats.values() if not s.below_min_cell]
    if eligible:
        max_rate = max(s.selection_rate for s in eligible) or 1.0
        for s in stats.values():
            if not s.below_min_cell:
                s.impact_ratio = round(s.selection_rate / max_rate, 3) if max_rate else None

    # Fisher's exact: each group vs the rest (2x2: selected/not x in-group/out-group).
    total_sel = int(sel.sum())
    total = len(sel)
    for g, s in stats.items():
        a = s.selected
        b = s.n - s.selected
        c = total_sel - s.selected
        d = (total - s.n) - (total_sel - s.selected)
        if min(a + b, c + d) > 0:
            try:
                _, p = fisher_exact([[a, b], [c, d]])
                s.fisher_p = round(float(p), 4)
            except ValueError:
                s.fisher_p = None

    report.groups = sorted(stats.values(), key=lambda s: s.group)
    ratios = [s.impact_ratio for s in report.groups if s.impact_ratio is not None]
    if ratios:
        report.min_impact_ratio = min(ratios)
        report.four_fifths_pass = report.min_impact_ratio >= 0.8
        if not report.four_fifths_pass:
            report.notes.append(
                f"min impact ratio {report.min_impact_ratio} < 0.80 (four-fifths rule) — disparate "
                f"impact flagged for human review."
            )
    for s in report.groups:
        if s.below_min_cell:
            report.notes.append(f"group '{s.group}' n={s.n} below min_cell={min_cell}; excluded from ratio")
    return report


def exposure_parity(labels: list[str | None], ranks: list[int], min_cell: int = 5) -> dict:
    """Rank-aware check: average position-discounted exposure (1/log2(rank+1)) per group. A model can
    pass top-k parity yet rank one group systematically lower; this catches that."""
    by_group: dict[str, list[float]] = {}
    for lab, rank in zip(labels, ranks):
        if lab is None:
            continue
        by_group.setdefault(lab, []).append(1.0 / math.log2(rank + 1) if rank >= 1 else 1.0)
    exposure = {g: float(np.mean(v)) for g, v in by_group.items() if len(v) >= min_cell}
    if not exposure:
        return {"exposure": {}, "parity_ratio": None}
    mx = max(exposure.values()) or 1.0
    return {
        "exposure": {g: round(e, 4) for g, e in exposure.items()},
        "parity_ratio": round(min(exposure.values()) / mx, 3),
    }


def homophily_disparity(
    candidate_labels: list[str | None],
    selected_mask: list[bool],
    reference_group: str,
    min_cell: int = 5,
) -> dict:
    """The reframed hunch, as a GUARDRAIL METRIC (never an input). Measures whether candidates whose
    group differs from a reference (e.g., the hiring team's dominant group) are selected at a lower
    rate than candidates who match it. A ratio well below 1.0 is evidence of the in-group/homophily
    bias the coordinator suspected — quantified so it can be corrected, not encoded."""
    same_n = same_s = diff_n = diff_s = 0
    for lab, sel in zip(candidate_labels, selected_mask):
        if lab is None:
            continue
        if lab == reference_group:
            same_n += 1
            same_s += int(bool(sel))
        else:
            diff_n += 1
            diff_s += int(bool(sel))
    if same_n < min_cell or diff_n < min_cell:
        return {"computable": False, "reason": "insufficient cohort size", "reference_group": reference_group}
    same_rate = same_s / same_n
    diff_rate = diff_s / diff_n
    ratio = round(diff_rate / same_rate, 3) if same_rate else None
    return {
        "computable": True,
        "reference_group": reference_group,
        "same_group_selection_rate": round(same_rate, 3),
        "different_group_selection_rate": round(diff_rate, 3),
        "disparity_ratio": ratio,  # < 0.8 => homophily bias flagged
        "flagged": (ratio is not None and ratio < 0.8),
    }
