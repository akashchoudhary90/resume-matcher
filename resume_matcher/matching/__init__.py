"""Matching engine: taxonomy normalization -> retrieval -> rerank -> LLM extraction ->
deterministic ranker -> coaching. The LLM never sets the score; ranker.py does (privilege
separation)."""
