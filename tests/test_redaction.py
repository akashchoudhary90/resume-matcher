from resume_matcher.inference.redaction import assert_redacted, redact_text


def test_redaction_strips_direct_identifiers():
    raw = (
        "Jane Doe\njane.doe@example.com\n+1 (416) 555-1234\n"
        "123 Main Street, Toronto  M5V 3A8\nhttps://linkedin.com/in/janedoe\nPython developer."
    )
    out = redact_text(raw, name="Jane Doe")
    assert "jane.doe@example.com" not in out
    assert "555-1234" not in out
    assert "Main Street" not in out
    assert "M5V 3A8" not in out
    assert "linkedin.com" not in out
    assert "Jane" not in out and "Doe" not in out
    assert "Python developer." in out  # non-PII content survives
    assert assert_redacted(out) == []  # tripwire passes


def test_redaction_catches_obfuscated_email_and_profile_urls():
    # #11: bracketed obfuscation + scheme-less profile links that the basic patterns missed.
    out = redact_text("jane [at] example [dot] com or github.com/janedoe and linkedin.com/in/jane")
    assert "[EMAIL]" in out and "example" not in out
    assert "janedoe" not in out and "linkedin.com/in/jane" not in out
    assert assert_redacted("contact jane (at) corp (dot) io") == ["email"]
    assert "url" in assert_redacted("portfolio at github.com/someone")


def test_redaction_does_not_eat_tech_domains():
    # ...but tech domains named as SKILLS (not contact links) must survive.
    assert "react.dev" in redact_text("Contributed to the react.dev documentation")
    assert "spring.io" in redact_text("Used spring.io guides extensively")


def test_assert_redacted_detects_leaks():
    assert "email" in assert_redacted("reach me at a@b.com")


def test_assert_redacted_detects_postal_code():
    # redact_text strips Canadian postal codes, so the tripwire must flag them too.
    assert "postal" in assert_redacted("Toronto, ON  M5V 3A8")


def test_redactor_and_tripwire_agree():
    # Whatever redact_text strips, assert_redacted must consider clean (no disagreement on what's PII).
    raw = (
        "Jane Doe — jane.doe@example.com — +1 (416) 555-1234\n"
        "123 Main Street, Toronto  M5V 3A8 — https://linkedin.com/in/janedoe"
    )
    assert assert_redacted(redact_text(raw, name="Jane Doe")) == []
