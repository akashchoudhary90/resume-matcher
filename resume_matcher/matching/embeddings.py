"""Text embeddings with a graceful fallback.

If `sentence-transformers` is installed, we use a real bi-encoder (semantic). Otherwise we fall back
to a dependency-free TF-IDF vectorizer implemented in NumPy, so retrieval still works in CI / on a
minimal install. The rest of the system only depends on `Embedder.encode`.
"""
from __future__ import annotations

import math
import re

import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class _TfidfEmbedder:
    """Minimal TF-IDF + L2 normalization. `fit` builds the vocab/idf from a corpus."""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray | None = None

    def fit(self, corpus: list[str]) -> "_TfidfEmbedder":
        df: dict[str, int] = {}
        for doc in corpus:
            for tok in set(_tokenize(doc)):
                df[tok] = df.get(tok, 0) + 1
        self.vocab = {tok: i for i, tok in enumerate(sorted(df))}
        n = max(1, len(corpus))
        idf = np.zeros(len(self.vocab), dtype=np.float64)
        for tok, i in self.vocab.items():
            idf[i] = math.log((1 + n) / (1 + df[tok])) + 1.0
        self.idf = idf
        return self

    def encode(self, texts: list[str]) -> np.ndarray:
        if self.idf is None:
            self.fit(texts)
        assert self.idf is not None
        mat = np.zeros((len(texts), len(self.vocab)), dtype=np.float64)
        for r, doc in enumerate(texts):
            toks = _tokenize(doc)
            if not toks:
                continue
            for tok in toks:
                j = self.vocab.get(tok)
                if j is not None:
                    mat[r, j] += 1.0
            mat[r] /= len(toks)
        mat *= self.idf
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms


class _StEmbedder:  # pragma: no cover - exercised only when sentence-transformers is installed
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)

    def fit(self, corpus: list[str]) -> "_StEmbedder":
        return self  # no corpus fitting needed

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True),
            dtype=np.float64,
        )


class Embedder:
    """Backend-agnostic embedder. Prefers sentence-transformers; falls back to TF-IDF."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", prefer_semantic: bool = True):
        self.backend = "tfidf"
        self._impl: _TfidfEmbedder | _StEmbedder
        if prefer_semantic:
            try:
                self._impl = _StEmbedder(model_name)
                self.backend = "sentence-transformers"
                return
            except Exception:
                pass
        self._impl = _TfidfEmbedder()

    def fit(self, corpus: list[str]) -> "Embedder":
        self._impl.fit(corpus)
        return self

    def encode(self, texts: list[str]) -> np.ndarray:
        return self._impl.encode(texts)


def cosine_scores(query_vec: np.ndarray, doc_matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity of a single (normalized) query vector against a (normalized) doc matrix."""
    q = query_vec.reshape(-1)
    return doc_matrix @ q
