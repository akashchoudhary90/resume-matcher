"""Proxy-leakage diagnostic.

Train an auxiliary classifier to predict a protected label from the scoring features/scores. If it
predicts well above the majority-class baseline, the features leak the protected attribute even
though it was never an input — gate every new feature through this before shipping.

Uses scikit-learn if available; otherwise a small NumPy logistic regression. Which one ran is
reported in `method` — a leakage number is only interpretable if you know what produced it. Binary
target: 1 if the candidate is in `target_group`, else 0.
"""
from __future__ import annotations

import numpy as np


def _np_logreg_auc(X: np.ndarray, y: np.ndarray, n_repeats: int = 9) -> tuple[float, float]:
    """Repeated random-split logistic regression, averaging accuracy + AUC. Averaging over splits
    is essential on small samples: a single split's AUC is high-variance and false-positives easily."""
    accs, aucs = [], []
    for seed in range(n_repeats):
        rng = np.random.default_rng(seed)
        n = len(y)
        idx = rng.permutation(n)
        split = max(1, int(0.7 * n))
        tr, te = idx[:split], idx[split:]
        if len(te) == 0:
            te = tr
        # Standardize FIRST, then append the intercept column: standardizing after the append
        # divides the constant column by its own (zero) spread and zeroes the intercept out — the
        # model then had no bias term and mis-scored any cohort whose base rate isn't 0.5.
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        Xtr = np.hstack([(X[tr] - mu) / sd, np.ones((len(tr), 1))])
        Xte = np.hstack([(X[te] - mu) / sd, np.ones((len(te), 1))])
        w = np.zeros(Xtr.shape[1])
        ytr = y[tr].astype(float)
        for _ in range(300):
            p = 1.0 / (1.0 + np.exp(-Xtr @ w))
            grad = Xtr.T @ (p - ytr) / len(ytr) + 1e-3 * w
            w -= 0.5 * grad
        scores = 1.0 / (1.0 + np.exp(-Xte @ w))
        accs.append(float(((scores >= 0.5).astype(int) == y[te]).mean()))
        aucs.append(_auc(y[te].astype(int), scores))
    return float(np.mean(accs)), float(np.mean(aucs))


def _auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Mann–Whitney U / (|pos|*|neg|), with MIDRANKS for ties.

    Tied scores must split the credit (0.5 per tied pair). Ordinal ranking instead breaks ties by
    array position — positives happen to be concatenated first, so every tie scored a full point for
    them. A classifier that outputs one constant for everyone (zero information) then measured
    AUC 1.0, which is the exact failure mode this diagnostic exists to catch."""
    pos = y_score[y_true == 1]
    neg = y_score[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    scores = np.concatenate([pos, neg])
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1, dtype=float)
    srt = scores[order]
    start = 0
    for i in range(1, len(srt) + 1):          # average each run of equal scores over its own ranks
        if i == len(srt) or srt[i] != srt[start]:
            if i - start > 1:
                ranks[order[start:i]] = (start + i + 1) / 2.0
            start = i
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def proxy_leakage(features: np.ndarray, labels: list[str | None], target_group: str) -> dict:
    """Return leakage diagnostics for predicting membership of `target_group` from `features`."""
    mask = np.array([lab is not None for lab in labels])
    X = np.asarray(features, dtype=float)[mask]
    y = np.array([1 if lab == target_group else 0 for lab in labels if lab is not None])
    if len(y) < 10 or y.sum() == 0 or y.sum() == len(y):
        return {"computable": False, "reason": "insufficient or single-class data"}

    baseline = max(y.mean(), 1 - y.mean())  # majority-class accuracy
    positives, negatives = int(y.sum()), int(len(y) - y.sum())
    # Which estimator ran is REPORTED, not swallowed: the old blanket `except Exception` meant a
    # silently-degraded fallback (or a real sklearn bug) was indistinguishable from a clean CV run in
    # the signed compliance pack. Stratified CV needs >=2 of each class per fold-set; below that the
    # fallback's repeated random splits are the honest estimator.
    method = "fallback_linear"
    cv = max(2, min(5, positives, negatives))
    if min(positives, negatives) >= 2:
        try:  # pragma: no cover - optional dep
            from sklearn.linear_model import LogisticRegression
            from sklearn.model_selection import cross_val_predict
            from sklearn.metrics import roc_auc_score
            method = "logreg_cv"
        except ImportError:
            pass

    if method == "logreg_cv":  # pragma: no cover - optional dep
        proba = cross_val_predict(LogisticRegression(max_iter=1000), X, y, cv=cv,
                                  method="predict_proba")[:, 1]
        acc = float(((proba >= 0.5).astype(int) == y).mean())
        auc = float(roc_auc_score(y, proba))
    else:
        acc, auc = _np_logreg_auc(X, y)

    lift = round(float(acc) - float(baseline), 3)
    return {
        "computable": True,
        "method": method,
        "target_group": target_group,
        "baseline_accuracy": round(float(baseline), 3),
        "classifier_accuracy": round(acc, 3),
        "auc": round(auc, 3),
        "accuracy_lift": lift,
        # AUC well above 0.5 (or accuracy well above baseline) => the features leak the attribute.
        "leakage": bool(auc >= 0.70 or lift >= 0.10),
    }
