"""Hidden-text detection — the highest-precision anti-gaming signal.

Two modes:
  * cross_modal_diff(visible, extracted): compares what a human sees vs what a parser extracts; any
    tokens present only in the extracted layer are hidden (white-on-white, 1pt, off-canvas).
  * scan_pdf(path): if pdfplumber is installed, flags characters with near-invisible size or
    background-matching color directly from the PDF.

Hidden text is the classic carrier for prompt-injection payloads. Flags route to human review.
"""
from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-zA-Z0-9+#.]+")


def _tokset(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def cross_modal_diff(visible_text: str, extracted_text: str, min_hidden: int = 3) -> list[str]:
    """Flag tokens that the parser extracted but a human would not see."""
    hidden = _tokset(extracted_text) - _tokset(visible_text)
    if len(hidden) >= min_hidden:
        sample = ", ".join(sorted(hidden)[:8])
        return [f"hidden_text:cross_modal:{len(hidden)} tokens ({sample})"]
    return []


def scan_pdf(path: str, min_font_size: float = 3.0) -> list[str]:  # pragma: no cover - optional dep
    """Flag near-invisible characters in a PDF (requires pdfplumber)."""
    try:
        import pdfplumber
    except ImportError:
        return ["hidden_text:scan_skipped:pdfplumber_not_installed"]

    tiny = 0
    white = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for ch in page.chars:
                if ch.get("size", 99) < min_font_size:
                    tiny += 1
                color = ch.get("non_stroking_color")
                if color in (1, (1, 1, 1), [1, 1, 1]):  # white text
                    white += 1
    flags = []
    if tiny:
        flags.append(f"hidden_text:tiny_font:{tiny}")
    if white:
        flags.append(f"hidden_text:white_text:{white}")
    return flags
