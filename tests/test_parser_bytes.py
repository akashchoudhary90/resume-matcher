"""In-memory upload parsing (parse_resume_bytes) — never touches disk; redacts at ingestion."""
import io

import pytest

from resume_matcher.ingestion.parser import (
    ParseError,
    extract_bytes_text,
    parse_resume_bytes,
)


def test_txt_bytes_parsed_and_redacted():
    data = b"Jane Doe\nEmail: jane@example.com Phone: (416) 555-1212\nPython and SQL developer."
    cand = parse_resume_bytes("R01", "jane.txt", data)
    assert cand.has_resume
    assert "jane@example.com" not in cand.text  # redaction ran at ingestion
    assert "[EMAIL]" in cand.text
    assert "python" in cand.skills and "sql" in cand.skills


def test_upload_redacts_inferred_name():
    # No name is supplied on the upload path; it must be inferred from the header and redacted.
    data = b"Jane Doe\nExperienced Python developer. Jane led SQL projects at scale."
    cand = parse_resume_bytes("R01", "resume.txt", data)
    assert "Jane" not in cand.text and "Doe" not in cand.text
    assert "[NAME]" in cand.text


def test_invisible_chars_stripped_at_ingestion():
    raw = "Python​developer‮ with \U000e0041hidden tags".encode("utf-8")
    cand = parse_resume_bytes("R01", "r.txt", raw)
    for ch in ("​", "‮", "\U000e0041"):
        assert ch not in cand.text


def test_auto_redact_name_false_keeps_name_but_still_redacts_contact():
    # The consented client demo keeps names (identifiable results) but still strips contact info.
    data = b"Jane Doe\njane@example.com (416) 555-1212\nPython and SQL developer."
    cand = parse_resume_bytes("R01", "jane.txt", data, auto_redact_name=False)
    assert "Jane" in cand.text and "Doe" in cand.text  # name kept
    assert "jane@example.com" not in cand.text and "555" not in cand.text  # contact still redacted


def test_unknown_extension_decoded_as_text():
    assert "hello" in extract_bytes_text("notes.rtfx", b"hello python")


def test_legacy_doc_rejected():
    with pytest.raises(ParseError):
        extract_bytes_text("old.doc", b"\xd0\xcf\x11\xe0garbage")


def test_pdf_bytes_when_backend_present():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    # A blank PDF yields no text -> empty string (not an error).
    assert extract_bytes_text("blank.pdf", buf.getvalue()) == ""


def test_docx_bytes_when_backend_present():
    pytest.importorskip("docx")
    import docx

    buf = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph("Python developer with SQL experience.")
    doc.save(buf)
    text = extract_bytes_text("resume.docx", buf.getvalue())
    assert "Python developer" in text


def test_missing_pdf_backend_raises_parse_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name in ("pdfplumber", "pypdf"):
            raise ImportError("simulated missing backend")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ParseError):
        extract_bytes_text("x.pdf", b"%PDF-1.4 ...")
