"""Inference layer: the swappable LLM boundary.

Everything in the system depends on `InferenceAdapter` and the Pydantic schema in this package —
never on a model SDK. Swap the backend with the RM_INFERENCE_BACKEND env var (see get_adapter()).
"""
