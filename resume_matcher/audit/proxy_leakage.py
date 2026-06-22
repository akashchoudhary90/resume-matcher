"""Proxy-leakage diagnostic.

Train an auxiliary classifier to predict a protected label from the scoring features/scores. If it
predicts well above the majority-class baseline, the features leak the protected attribute even
though it was never an input — gate every new feature through this before shipping.

Uses scikit-learn if available; otherwise a small NumPy logistic regression. Binary target: 1 if the
candidate is in `target_group`, else 0.
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
        Xtr = np.hstack([X[tr], np.ones((len(tr), 1))])
        Xte = np.hstack([X[te], np.ones((len(te), 1))])
        mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        Xtr = (Xtr - mu) / sd
        Xte = (Xte - mu) / sd
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
    pos = y_score[y_true == 1]
    neg = y_score[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    # Mann–Whitney U statistic / (|pos|*|neg|)
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
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
    try:  # pragma: no cover - optional dep
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_predict
        from sklearn.metrics import roc_auc_score

        clf = LogisticRegression(max_iter=1000)
        proba = cross_val_predict(clf, X, y, cv=min(5, int(y.sum())), method="predict_proba")[:, 1]
        acc = float(((proba >= 0.5).astype(int) == y).mean())
        auc = float(roc_auc_score(y, proba))
    except Exception:
        acc, auc = _np_logreg_auc(X, y)

    lift = round(float(acc) - float(baseline), 3)
    return {
        "computable": True,
        "target_group": target_group,
        "baseline_accuracy": round(float(baseline), 3),
        "classifier_accuracy": round(acc, 3),
        "auc": round(auc, 3),
        "accuracy_lift": lift,
        # AUC well above 0.5 (or accuracy well above baseline) => the features leak the attribute.
        "leakage": bool(auc >= 0.70 or lift >= 0.10),
    }
