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


def scan_pdf_bytes(data: bytes, min_font_size: float = 3.0) -> list[str]:
    """scan_pdf for in-memory PDF bytes (the demo's text path never writes uploads to disk)."""
    import io

    return scan_pdf(io.BytesIO(data), min_font_size=min_font_size)


def _color_values(color) -> list[float] | None:
    if isinstance(color, (int, float)):
        return [float(color)]
    if isinstance(color, (list, tuple)):
        try:
            return [float(v) for v in color]
        except (TypeError, ValueError):
            return None
    return None


def _near_invisible_color(color) -> bool:
    """Near-paper-white fill: unreadable on a white page even though it isn't EXACT white.
    Handles gray scalars/1-tuples, RGB, and CMYK (where white is all-zero ink)."""
    vals = _color_values(color)
    if not vals:
        return False
    if len(vals) == 4:  # CMYK
        return all(v <= 0.05 for v in vals)
    return all(v >= 0.9 for v in vals)


def _char_hidden(ch, page, min_font_size: float) -> bool:
    """A char a human reader would not see: tiny, near-invisible fill, or drawn off-canvas."""
    if ch.get("size", 99) < min_font_size:
        return True
    if _near_invisible_color(ch.get("non_stroking_color")):
        return True
    return (ch.get("x1", 1) <= 0 or ch.get("x0", 0) >= page.width
            or ch.get("bottom", 1) <= 0 or ch.get("top", 0) >= page.height)


def scan_pdf(path, min_font_size: float = 3.0, min_hidden_tokens: int = 12) -> list[str]:
    """Flag near-invisible characters in a PDF (requires pdfplumber). `path` may be a filesystem
    path or a binary file-like object (pdfplumber accepts both).

    Two layers of detection:
      * per-char counters — tiny fonts and EXACT-white text (the classic carriers, kept as-is);
      * an invisible-layer token diff — chars that are near-paper-white, CMYK-white, tiny, or drawn
        fully off-canvas are stripped into a "visible projection" of each page; when the tokens
        present ONLY in the hidden remainder reach `min_hidden_tokens` DISTINCT tokens, the
        `hidden_text:invisible_layer` flag fires (and down-weights via the ranker's integrity
        gate). The distinct-token threshold keeps precision high: a light-gray watermark repeats a
        handful of tokens; a keyword-stuffing payload needs many.
    Not detected here: text occluded by an image/rectangle painted over it (needs z-order
    analysis) — the vision path remains the guard for that class."""
    try:
        import pdfplumber
    except ImportError:
        return ["hidden_text:scan_skipped:pdfplumber_not_installed"]

    tiny = 0
    white = 0
    full_parts: list[str] = []
    visible_parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for ch in page.chars:
                if ch.get("size", 99) < min_font_size:
                    tiny += 1
                if ch.get("non_stroking_color") in (1, (1, 1, 1), [1, 1, 1]):  # exact white
                    white += 1
            full_parts.append(page.extract_text() or "")
            # Visible projection: the page with hidden chars filtered out. Word segmentation is
            # pdfplumber's, so the token diff below compares like with like.
            vis = page.filter(
                lambda obj, _p=page: obj.get("object_type") != "char"
                or not _char_hidden(obj, _p, min_font_size)
            )
            visible_parts.append(vis.extract_text() or "")
    flags = []
    if tiny:
        flags.append(f"hidden_text:tiny_font:{tiny}")
    if white:
        flags.append(f"hidden_text:white_text:{white}")
    hidden_only = _tokset("\n".join(full_parts)) - _tokset("\n".join(visible_parts))
    if len(hidden_only) >= min_hidden_tokens:
        sample = ", ".join(sorted(hidden_only)[:8])
        flags.append(f"hidden_text:invisible_layer:{len(hidden_only)} tokens ({sample})")
    return flags
