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
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..inference.adapter import InferenceAdapter, get_adapter
from ..inference.schema import CandidateProfile
from ..ingestion.job_posting import build_job_spec
from ..ingestion.parser import infer_education_level, infer_years_experience
from ..matching.evaluator import evaluate
from ..matching.taxonomy import normalize_skills

EVAL_DIR = Path(__file__).resolve().parents[2] / "data" / "eval"
DATA = EVAL_DIR / "labeled_examples.json"

# Named eval sets. coordinator_ratings is the PRIMARY, realistic regression target (holistic
# recruiter-style ratings on resumes that demonstrate skills WITHOUT keyword-matching); the keyword
# mock scores POORLY on it, which is the honest signal. labeled_examples is a clear-cut seed used only
# as a pipeline smoke/sanity check — its ~perfect score is NOT a measure of real-world accuracy.
NAMED_DATASETS = {
    "coordinator": EVAL_DIR / "coordinator_ratings.json",
    "labeled": EVAL_DIR / "labeled_examples.json",
}
PRIMARY_DATASET = "coordinator"


def resolve_dataset(name_or_path: str | Path | None) -> Path:
    """Map a friendly name ('coordinator' | 'labeled') or a path to a concrete file; default PRIMARY."""
    if name_or_path is None:
        return NAMED_DATASETS[PRIMARY_DATASET]
    key = str(name_or_path)
    return NAMED_DATASETS[key] if key in NAMED_DATASETS else Path(name_or_path)

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


def run_benchmark(
    examples: list[dict], adapter: InferenceAdapter | None = None, progress=None
) -> dict:
    """Score every example and report agreement metrics. Examples are scored CONCURRENTLY (up to
    RM_EVAL_WORKERS, default 8) — a real LLM backend spends the whole call waiting on the model, and
    the claude_cli adapter already caps actual parallel processes via its own semaphore, so this just
    stops the harness from making 24 slow calls strictly one at a time. Results are reassembled in
    dataset order, so output is deterministic for a deterministic backend regardless of workers.
    `progress(done, total)` is called after each example (for a live counter)."""
    adapter = adapter or get_adapter("mock")
    total = len(examples)
    workers = max(1, min(int(os.environ.get("RM_EVAL_WORKERS", "8") or "8"), total or 1))
    results: list = [None] * total
    done = 0
    if workers == 1:
        for i, ex in enumerate(examples):
            results[i] = score_example(ex, adapter)
            done += 1
            if progress:
                progress(done, total)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(score_example, ex, adapter): i for i, ex in enumerate(examples)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()  # a backend error propagates -> aborts cleanly
                done += 1
                if progress:
                    progress(done, total)
    rows = []
    for i, ex in enumerate(examples):
        fit, grade = results[i]
        h = ex.get("human", {})
        rows.append({
            "id": ex.get("id"), "job": ex.get("job", {}).get("title"),
            "tool_fit": fit, "tool_grade": grade, "tool_label": fit_to_label(fit),
            "human_label": h.get("label"), "human_score": h.get("score"),
        })
    return {"rows": rows, "metrics": _metrics(rows)}


def _agg(values: list) -> dict | None:
    """Aggregate a metric across runs: mean/stdev/min/max over the finite, non-None values (None
    when a metric was undefined every run, e.g. spearman with <3 scored examples, or nan from a
    constant-input correlation). Population stdev computed directly (statistics.pstdev has a
    float/Fraction edge case on plain floats), so a single run honestly reports 0.0 spread."""
    nums = [float(v) for v in values
            if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not nums:
        return None
    mean = sum(nums) / len(nums)
    sd = math.sqrt(sum((x - mean) ** 2 for x in nums) / len(nums))
    return {"mean": round(mean, 3), "stdev": round(sd, 3),
            "min": round(min(nums), 3), "max": round(max(nums), 3), "n": len(nums)}


_AGG_METRICS = ("label_accuracy", "within_one_bucket", "spearman", "mae")


def run_benchmark_repeated(
    examples: list[dict], adapter: InferenceAdapter | None = None, runs: int = 3, progress=None
) -> dict:
    """Run the SAME benchmark `runs` times and report per-metric mean/stdev/min/max.

    The whole point of measuring the REAL engine: an LLM backend is non-deterministic (no
    temperature/seed pinning yet), so a single run's number is a point estimate with unknown spread.
    Running N times and reporting the spread tells you whether a metric is trustworthy and how tight
    a regression floor can honestly be. Deterministic backends (mock) report stdev 0 across runs —
    which is exactly how the tests verify the aggregation without needing the model."""
    adapter = adapter or get_adapter("mock")
    runs = max(1, int(runs))
    per_run = []
    for r in range(runs):
        # progress(run_no, done, total) -> the caller can render "run 2/3: 5/24".
        cb = (lambda d, t, _r=r: progress(_r + 1, d, t)) if progress else None
        per_run.append(run_benchmark(examples, adapter, progress=cb))
    aggregate = {k: _agg([r["metrics"].get(k) for r in per_run]) for k in _AGG_METRICS}
    # Per-example fit-score spread across runs — surfaces WHICH resumes the engine is unstable on.
    fit_by_id: dict = {}
    for r in per_run:
        for row in r["rows"]:
            fit_by_id.setdefault(row["id"], []).append(row["tool_fit"])
    per_example_fit = {rid: _agg(vals) for rid, vals in fit_by_id.items()}
    return {
        "runs": runs,
        "n": per_run[0]["metrics"]["n"],
        "n_labeled": per_run[0]["metrics"]["n_labeled"],
        "metrics_by_run": [r["metrics"] for r in per_run],
        "aggregate": aggregate,
        "per_example_fit": per_example_fit,
        "last_rows": per_run[-1]["rows"],
        "confusion": per_run[-1]["metrics"]["confusion"],
    }


def load_examples(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else DATA
    return json.loads(p.read_text(encoding="utf-8")).get("examples", [])
