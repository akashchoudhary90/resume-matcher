"""Measurement harness — does the ranking agree with human judgment?

This is how you make accuracy *provable* (and tunable) instead of guessed. You collect labeled
examples — a job + a resume + a human rating (strong / ok / weak, optionally a 0-100 score) — and this
runs the SAME scoring pipeline the app uses and reports agreement metrics:

  * label_accuracy      — exact agreement with the human strong/ok/weak bucket
  * within_one_bucket    — off by at most one bucket (ordinal tolerance)
  * spearman             — rank correlation between tool fit and the human 0-100 score
  * mae                  — mean absolute error vs the human score
  * confusion            — human label -> tool label counts

Workflow: label ~20-30 pairs (data/eval/labeled_examples.json), run `python scripts/eval_accuracy.py`,
then adjust the ranker weights (matching/ranker.py constants) and re-run to see the metrics move.
Later, replace human ratings with real OUTCOMES (who was shortlisted/hired) to calibrate.

Runs on the deterministic mock backend by default, so results are reproducible (no LLM/network).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..inference.adapter import InferenceAdapter, get_adapter
from ..inference.schema import CandidateProfile
from ..ingestion.job_posting import build_job_spec
from ..ingestion.parser import infer_education_level, infer_years_experience
from ..matching.evaluator import evaluate
from ..matching.taxonomy import normalize_skills

DATA = Path(__file__).resolve().parents[2] / "data" / "eval" / "labeled_examples.json"

_LABEL_ORDER = {"weak": 0, "ok": 1, "strong": 2}
# Tool fit -> bucket. Aligns with the A/B/C/D grade bands (strong >= B, weak < C).
_STRONG_MIN, _OK_MIN = 65.0, 45.0


def fit_to_label(fit: float) -> str:
    return "strong" if fit >= _STRONG_MIN else "ok" if fit >= _OK_MIN else "weak"


def _candidate(text: str) -> CandidateProfile:
    return CandidateProfile(
        candidate_id="X", text=text, skills=normalize_skills(text),
        education_level=infer_education_level(text),
        years_experience=infer_years_experience(text), has_resume=bool(text.strip()),
    )


def _job_of(ex: dict):
    j = ex.get("job", {})
    return build_job_spec(
        job_id=str(ex.get("id", "J")), title=j.get("title", ""), employer=j.get("employer", ""),
        description=j.get("description", ""), required_skills=j.get("required_skills"),
        preferred_skills=j.get("preferred_skills"), must_have_skills=j.get("must_have_skills"),
        min_education=j.get("min_education"), min_years=j.get("min_years"),
    )


def score_example(ex: dict, adapter: InferenceAdapter) -> tuple[float, str]:
    res = evaluate(_candidate(ex.get("resume_text", "")), _job_of(ex), adapter)
    return res.fit_score, res.grade


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        from scipy.stats import spearmanr
        r = spearmanr(xs, ys)
        corr = getattr(r, "statistic", None)
        if corr is None:
            corr = getattr(r, "correlation", None)
        return round(float(corr), 3) if corr is not None else None
    except Exception:  # noqa: BLE001 - scipy missing / degenerate input
        return None


def _metrics(rows: list[dict]) -> dict:
    labeled = [r for r in rows if r.get("human_label") in _LABEL_ORDER]
    acc = within1 = None
    confusion: dict[str, dict[str, int]] = {}
    if labeled:
        acc = round(sum(r["tool_label"] == r["human_label"] for r in labeled) / len(labeled), 3)
        within1 = round(sum(
            abs(_LABEL_ORDER[r["tool_label"]] - _LABEL_ORDER[r["human_label"]]) <= 1 for r in labeled
        ) / len(labeled), 3)
        for r in labeled:
            confusion.setdefault(r["human_label"], {}).setdefault(r["tool_label"], 0)
            confusion[r["human_label"]][r["tool_label"]] += 1

    scored = [(r["tool_fit"], r["human_score"]) for r in rows if isinstance(r.get("human_score"), (int, float))]
    spearman = _spearman([a for a, _ in scored], [b for _, b in scored]) if len(scored) >= 3 else None
    mae = round(sum(abs(a - b) for a, b in scored) / len(scored), 2) if scored else None
    return {
        "n": len(rows), "n_labeled": len(labeled),
        "label_accuracy": acc, "within_one_bucket": within1,
        "spearman": spearman, "mae": mae, "confusion": confusion,
    }


def run_benchmark(examples: list[dict], adapter: InferenceAdapter | None = None) -> dict:
    adapter = adapter or get_adapter("mock")
    rows = []
    for ex in examples:
        fit, grade = score_example(ex, adapter)
        h = ex.get("human", {})
        rows.append({
            "id": ex.get("id"), "job": ex.get("job", {}).get("title"),
            "tool_fit": fit, "tool_grade": grade, "tool_label": fit_to_label(fit),
            "human_label": h.get("label"), "human_score": h.get("score"),
        })
    return {"rows": rows, "metrics": _metrics(rows)}


def load_examples(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else DATA
    return json.loads(p.read_text(encoding="utf-8")).get("examples", [])
