"""The invisible-layer guard for text-routed PDFs.

Moving text-layer PDFs off the vision path (grid fast path) removed the cross-modal backstop, so
scan_pdf gained an invisible-layer token diff: near-white / CMYK-white / off-canvas / tiny text
whose tokens never appear visibly. These tests build real PDFs byte-by-byte and prove the attack
class found in review (0.9-gray and off-canvas keyword stuffing scoring 100/A with a Clean badge)
is now flagged AND down-weighted — while light-gray watermarks stay clean (precision guard).
"""
import pytest

pytest.importorskip("pdfplumber")

from resume_matcher.antigaming.hidden_text import scan_pdf_bytes  # noqa: E402
from resume_matcher.api import demo as demo_mod  # noqa: E402
from resume_matcher.api.demo import SessionStore, _vision_candidate, run_demo  # noqa: E402
from resume_matcher.inference.adapters import claude_cli as claude_cli_mod  # noqa: E402

VISIBLE_LINES = [
    "Barista with five years of espresso experience and excellent customer service.",
    "Managed the morning rush, trained three junior staff, kept the bar spotless.",
    "Known for latte art, friendly service, and reliable opening shifts every week.",
]
STUFFED_LINE = ("python kubernetes terraform docker ansible jenkins graphql postgres "
                "redis kafka spark airflow")  # 12 distinct hidden-only tokens
WATERMARK = "scanned by camscanner page one"   # 5 distinct tokens -> below the precision threshold


def _pdf(runs):
    """Minimal valid one-page PDF. `runs` = [(color_ops, x, y, text), ...]; color_ops like
    '0 0 0 rg' (black) or '0.99 0.99 0.99 rg' (near-white)."""
    ops = "".join(f"BT /F1 12 Tf {color} 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n"
                  for color, x, y, text in runs)
    stream = ops.encode("latin-1")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        None,  # the content stream
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        if body is None:
            out += f"{i} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            out += stream + b"\nendstream\nendobj\n"
        else:
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def _visible_runs():
    return [("0 0 0 rg", 72, 720 - 20 * i, line) for i, line in enumerate(VISIBLE_LINES)]


def test_near_white_stuffing_flagged():
    pdf = _pdf(_visible_runs() + [("0.99 0.99 0.99 rg", 72, 400, STUFFED_LINE)])
    flags = scan_pdf_bytes(pdf)
    assert any(f.startswith("hidden_text:invisible_layer:") for f in flags), flags


def test_off_canvas_stuffing_flagged():
    # Plain black text, drawn 500pt below the page bottom — invisible to any reader.
    pdf = _pdf(_visible_runs() + [("0 0 0 rg", 72, -500, STUFFED_LINE)])
    flags = scan_pdf_bytes(pdf)
    assert any(f.startswith("hidden_text:invisible_layer:") for f in flags), flags


def test_gray_watermark_stays_clean():
    # A near-white watermark repeats a handful of tokens — far below the 12-distinct-token bar.
    pdf = _pdf(_visible_runs() + [("0.95 0.95 0.95 rg", 200, 400, WATERMARK)])
    flags = scan_pdf_bytes(pdf)
    assert not any(f.startswith("hidden_text:invisible_layer") for f in flags), flags


def test_clean_pdf_no_flags():
    assert scan_pdf_bytes(_pdf(_visible_runs())) == []


def test_text_routed_stuffed_pdf_is_downweighted_end_to_end(monkeypatch):
    """The review's exact attack: text-layer PDF, visible barista text, near-white dev keywords.
    It must route to the TEXT path (that's the fast path) yet come back flagged + down-weighted."""
    monkeypatch.setattr(claude_cli_mod, "available", lambda: True)

    def _no_cli(*a, **k):
        raise claude_cli_mod.InferenceError("hermetic test — no real CLI")

    monkeypatch.setattr(claude_cli_mod, "_run_cli", _no_cli)  # extraction falls back to mock

    pdf = _pdf(_visible_runs() + [("0.99 0.99 0.99 rg", 72, 400, STUFFED_LINE)])
    use_vision, independent = _vision_candidate("resume.pdf", ".pdf", pdf)
    assert use_vision is False and len(independent) >= demo_mod.VISION_MIN_TEXT  # text path

    sess = run_demo(store=SessionStore(ttl_seconds=600), backend="claude_cli",
                    required_skills=["python", "docker", "kubernetes"],
                    files=[("resume.pdf", pdf)])
    res = sess.results[0]
    assert any(f.startswith("hidden_text:invisible_layer") for f in res["flags"]), res["flags"]
    ex = res["explanation"]
    assert ex["integrity_factor"] < 1.0  # bounded down-weight, never auto-reject
    assert res["fit_score"] < 100.0


def test_hybrid_pdf_with_scanned_page_routes_to_vision(monkeypatch):
    # A text page + an image-only page: the whole-document threshold passes, but the scanned page
    # would silently never be read on the text path — the hybrid detector must force vision.
    import pdfplumber

    class _FakePage:
        def __init__(self, images, text):
            self.images, self._text = images, text

        def extract_text(self):
            return self._text

    class _FakePdf:
        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pdfplumber, "open",
                        lambda *_a, **_k: _FakePdf([_FakePage([], "text page " * 50),
                                                    _FakePage([{"an": "image"}], "")]))
    assert demo_mod._pdf_has_unreadable_page(b"%PDF-fake") is True
    monkeypatch.setattr(pdfplumber, "open",
                        lambda *_a, **_k: _FakePdf([_FakePage([], "text page " * 50)]))
    assert demo_mod._pdf_has_unreadable_page(b"%PDF-fake") is False


def test_grid_fast_path_skipped_when_cache_too_small(monkeypatch):
    from resume_matcher.api.demo import run_demo_grid

    monkeypatch.setattr(claude_cli_mod, "available", lambda: True)
    monkeypatch.setattr(claude_cli_mod, "_run_cli",
                        lambda *a, **k: (_ for _ in ()).throw(
                            claude_cli_mod.InferenceError("hermetic")))
    calls = []
    monkeypatch.setattr(claude_cli_mod, "extract_multi", lambda cand, jobs: calls.append(1) or {})
    monkeypatch.setattr(demo_mod, "CACHE_MAX", 4)  # cannot hold 2 roles x 2 resumes + headroom
    sess = run_demo_grid(
        store=SessionStore(ttl_seconds=600), backend="claude_cli",
        jobs=[{"title": "Dev", "job_text": "Python developer with SQL."},
              {"title": "Analyst", "job_text": "Excel analyst with SQL."}],
        files=[("a.txt", b"python sql excel " * 10), ("b.txt", b"sql excel python! " * 10)])
    assert calls == []                                       # batching skipped, not half-done
    assert len(sess.to_dict()["grid"]["candidates"]) == 2    # grid still completes per-cell