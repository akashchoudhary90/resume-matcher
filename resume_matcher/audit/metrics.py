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
    total_sel = int(sel.sum())
    if eligible:
        # No `or 1.0` fallback: an all-zero cohort has NO defined reference rate, and inventing one
        # manufactured a ratio (and a verdict) out of nothing. It is reported as such below.
        max_rate = max(s.selection_rate for s in eligible)
        if max_rate:
            for s in stats.values():
                if not s.below_min_cell:
                    s.impact_ratio = round(s.selection_rate / max_rate, 3)

    # Fisher's exact: each group vs the rest (2x2: selected/not x in-group/out-group).
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
    # A verdict needs something to compare: one group is a ratio against itself, and a cohort with no
    # selections at all has no reference rate. Both stay None (undecided) rather than passing.
    if total_sel == 0:
        report.notes.append("no selections yet — audit not meaningful")
    elif len(eligible) < 2:
        report.notes.append("fewer than 2 comparable groups")
    elif ratios:
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


def access_disparity(numerator: dict[str, int], denominator: dict[str, int],
                     min_cell: int = 5) -> dict:
    """Warm-intro ACCESS/CONVERSION disparity from two INDEPENDENT aggregate count dicts (never an
    aligned per-person label list — boundary #2). `denominator` = group sizes among the cohort
    (e.g. all applicants who self-ID'd); `numerator` = group sizes among those who got/converted an
    intro. A group whose DENOMINATOR is below min_cell is excluded from ratios; a group with a
    denominator but a suppressed numerator is reported as 'below threshold', NEVER as 0 (a
    suppressed small numerator is not the same as no one — the privacy nuance). Pure Python; no
    per-person data crosses the plane boundary.

    A suppressed numerator is unknown but BOUNDED: the cell was hidden precisely because it held at
    most min_cell-1 members, so the group's true rate lies in [0, (min_cell-1)/denom]. Suppression
    therefore never buys a PASS (A4): a bounded group whose ceiling is under the four-fifths line
    fails wherever its true rate sits, and one whose ceiling clears the line is *indeterminate*
    (its floor is 0) — a verdict a human has to reach with un-suppressed counts."""
    rates: dict[str, dict] = {}
    for group, denom in denominator.items():
        if denom < min_cell:
            continue
        num = numerator.get(group)
        if num is None:
            rates[group] = {"rate": None, "rate_bound_upper": round((min_cell - 1) / denom, 3),
                            "note": "below reporting threshold", "denom": denom}
        else:
            rates[group] = {"rate": round(num / denom, 3), "denom": denom, "num": num}
    computable = {g: r["rate"] for g, r in rates.items() if r.get("rate") is not None}
    bounded = {g: r["rate_bound_upper"] for g, r in rates.items() if r.get("rate") is None}
    notes: list[str] = []
    min_ratio: float | None = None
    four_fifths_pass: bool | None = None
    if not computable or len(rates) < 2:
        notes.append("fewer than 2 comparable groups")
    elif not (mx := max(computable.values())):
        notes.append("no access in any comparable group — audit not meaningful")
    else:
        threshold = 0.8 * mx
        min_ratio = round(min(computable.values()) / mx, 3)
        failed_bounds = sorted(g for g, ub in bounded.items() if ub < threshold)
        if failed_bounds or min_ratio < 0.8:
            four_fifths_pass = False
            if failed_bounds:
                notes.append("suppressed group cannot exceed 0.8 threshold — human review required")
        elif bounded:
            notes.append("indeterminate under suppression — human review required")
        else:
            four_fifths_pass = True
    return {
        "rates": rates,
        "min_impact_ratio": min_ratio,   # over the COMPUTABLE groups only
        "four_fifths_pass": four_fifths_pass,   # None when undecidable, never a suppression pass
        "min_cell": min_cell,
        "notes": notes,
    }


_ORIGIN_STAGES = ("accepted", "shortlisted", "hired")


def origin_impact(counts: dict[str, dict[str, int]], min_cell: int = 5) -> dict:
    """Does a BRIDGED intro (one routed over an alumni/mentorship hop) convert like an organic one?

    Takes two INDEPENDENT aggregate count dicts — {"organic": {...}, "bridged": {...}}, each with
    requested/accepted/shortlisted/hired — and reports per-stage conversion off `requested` plus the
    bridged/organic ratio. This is the honest read on whether the bridge actually helps the students
    it was built for, or just moves them one step further into the same funnel. Pure Python; counts
    only.

    EVERY published cell is min-cell suppressed, not just the ratios: a ratio over a handful of
    intros is noise, but the COUNT under it ('3 bridged intros, 1 hired') is a re-identifier once a
    coordinator holds a named roster. So, mirroring AuditDB.aggregate:
      * a stage `n` below min_cell publishes None, never the count, and its `rate` goes with it —
        an exact rate beside an exact `requested` is the suppressed n by multiplication;
      * `requested` below min_cell publishes None (the cohort itself is the small cell);
      * COMPLEMENTARY SUPPRESSION — once any stage is hidden, an exact `requested` narrows it
        (0..min_cell-1 out of a known denominator), so `requested` degrades to a min_cell-wide band
        string ('50-55'). Callers must treat it as opaque, not arithmetic.
    Ratios are still computed off the true counts internally; only the egress is redacted."""
    raw: dict[str, dict] = {}
    for origin in ("organic", "bridged"):
        row = counts.get(origin) or {}
        requested = int(row.get("requested", 0) or 0)
        stages = {}
        for stage in _ORIGIN_STAGES:
            n = int(row.get(stage, 0) or 0)
            # rate needs BOTH a publishable denominator and a publishable numerator
            rate = round(n / requested, 3) if requested >= min_cell and n >= min_cell else None
            stages[stage] = {"n": n, "rate": rate}
        raw[origin] = {"requested": requested, "stages": stages}

    ratios: dict[str, dict] = {}
    for stage in _ORIGIN_STAGES:
        org, brd = raw["organic"]["stages"][stage], raw["bridged"]["stages"][stage]
        # rate is None exactly when requested or n is below min_cell, so this still gates on both
        if org["rate"] is None or brd["rate"] is None or not org["rate"]:
            ratios[stage] = {"ratio": None, "note": "below threshold"}
        else:
            ratios[stage] = {"ratio": round(brd["rate"] / org["rate"], 3)}

    out: dict[str, dict] = {}
    for origin, row in raw.items():
        stages = {stage: {"n": (cell["n"] if cell["n"] >= min_cell else None), "rate": cell["rate"]}
                  for stage, cell in row["stages"].items()}
        suppressed = sum(1 for cell in stages.values() if cell["n"] is None)
        published: dict = {"requested": row["requested"], "stages": stages,
                           "suppressed_stages": suppressed}
        if row["requested"] < min_cell:
            published["requested"] = None
            published["note"] = "cohort below reporting threshold"
        elif suppressed:
            lo = (row["requested"] // min_cell) * min_cell
            published["requested"] = f"{lo}-{lo + min_cell}"
            # Banding is cosmetic defence-in-depth, NOT an information barrier: the stages are a
            # nested funnel, so a visible stage's n with its 3-dp rate re-derives the exact
            # denominator out of this band. The min-cell guarantee is carried entirely by the rule
            # above — rate is suppressed whenever its n is — which pins a hidden stage at
            # 0..min_cell-1 even with the exact denominator known. Don't mistake this for the
            # protection: strengthening it means suppressing more cells, not widening the band.
            published["note"] = "total banded (display only; suppression is what protects the cell)"
        out[origin] = published
    return {"by_origin": out, "bridged_over_organic": ratios, "min_cell": min_cell}
