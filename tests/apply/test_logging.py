"""
tests/apply/test_logging.py — RED tests for Shard S16 (logging-scrubber).

Follows spec §TDD scaffolding verbatim, plus a `test_realistic_pii_shapes_all_redacted`
that binds real-shaped names/emails/phones to surface any regex hole
(parent-directed extra scrutiny).
"""
from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apply import logging as apply_logging  # noqa: E402
from apply.logging import (  # noqa: E402
    _ALLOWED_EVENT_NAMES,
    _PII_KEY_RE,
    _REDACTED,
    install_scrubber,
    lint_event_name,
    scrub_kv,
)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_structlog_and_module_state():
    """Reset structlog config and module-level sentinels before each test."""
    structlog.reset_defaults()
    apply_logging._installed = False
    apply_logging._seen_unknown_events.clear()
    yield
    structlog.reset_defaults()
    apply_logging._installed = False
    apply_logging._seen_unknown_events.clear()


# --- 1. idempotency --------------------------------------------------------


def test_install_is_idempotent():
    """Call install_scrubber() 5 times. scrub_kv appears exactly once."""
    for _ in range(5):
        install_scrubber()
    procs = structlog.get_config()["processors"]
    matches = [p for p in procs if p is scrub_kv]
    assert len(matches) == 1, f"expected exactly one scrub_kv, got {len(matches)} in {procs}"
    lint_matches = [p for p in procs if p is lint_event_name]
    assert len(lint_matches) == 1, f"expected exactly one lint_event_name, got {len(lint_matches)}"


def test_install_idempotent_10x_identity():
    """10 install calls — processor list stays stable in identity."""
    install_scrubber()
    snapshot = list(structlog.get_config()["processors"])
    for _ in range(10):
        install_scrubber()
    after = list(structlog.get_config()["processors"])
    assert snapshot == after
    assert sum(1 for p in after if p is scrub_kv) == 1
    assert sum(1 for p in after if p is lint_event_name) == 1


# --- 2/3. key redaction ----------------------------------------------------


def test_email_key_redacted():
    out = scrub_kv(None, "info", {"event": "evt", "email": "secret@example.com"})
    assert out["email"] == _REDACTED == "***REDACTED***"
    assert out["event"] == "evt"


def test_substring_matches_email_variants():
    out = scrub_kv(
        None,
        "info",
        {
            "event": "evt",
            "user_email": "a@x.com",
            "candidate_email": "b@x.com",
            "EmailAddress": "c@x.com",
        },
    )
    assert out["user_email"] == _REDACTED
    assert out["candidate_email"] == _REDACTED
    assert out["EmailAddress"] == _REDACTED


def test_phone_key_redacted():
    out = scrub_kv(None, "info", {"event": "evt", "phone": "555-0100", "phone_raw": "raw"})
    assert out["phone"] == _REDACTED
    assert out["phone_raw"] == _REDACTED


def test_all_ten_required_keys_covered():
    """L7 mitigation must cover all 10 spec keys, case-insensitively."""
    kv = {
        "email": "a@x.com",
        "phone": "555-0100",
        "first_name": "Jane",
        "last_name": "Doe",
        "address": "1 Main St",
        "linkedin": "url",
        "answer": "yes",
        "prompt": "why?",
        "raw": "xyz",
        "value": 42,
    }
    out = scrub_kv(None, "info", {"event": "evt", **kv})
    for k in kv:
        assert out[k] == _REDACTED, f"{k} was not redacted"


# --- 4. nested one-level recursion ------------------------------------------


def test_nested_dict_one_level_redacted():
    out = scrub_kv(
        None,
        "info",
        {
            "event": "evt",
            "filled_fields": {
                "email": "secret@example.com",
                "company": "Acme",
                "linkedin": "lnk",
            },
        },
    )
    assert out["filled_fields"]["email"] == _REDACTED
    assert out["filled_fields"]["linkedin"] == _REDACTED
    assert out["filled_fields"]["company"] == "Acme"


def test_nested_dict_two_levels_not_redacted():
    out = scrub_kv(None, "info", {"event": "evt", "outer": {"inner": {"email": "y@x.com"}}})
    # Two levels deep — spec §criterion 4 documents only one-level recursion.
    assert out["outer"]["inner"]["email"] == "y@x.com"


# --- 5. message body boundary ----------------------------------------------


def test_message_body_not_scrubbed():
    """Caller-bug boundary: PII in event message string passes through."""
    out = scrub_kv(None, "info", {"event": "emailed secret@example.com"})
    assert out["event"] == "emailed secret@example.com"


# --- non-string / non-dict values ------------------------------------------


def test_list_value_redacted_whole():
    out = scrub_kv(None, "info", {"event": "evt", "answers": ["yes", "no"]})
    assert out["answers"] == _REDACTED


def test_none_value_passthrough_when_key_matches():
    out = scrub_kv(None, "info", {"event": "evt", "email": None})
    assert out["email"] is None


def test_int_value_redacted_when_key_matches():
    out = scrub_kv(None, "info", {"event": "evt", "phone": 15550100})
    assert out["phone"] == _REDACTED


# --- 6. event-name allowlist -----------------------------------------------


def test_allowed_event_name_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="apply.logging"):
        lint_event_name(None, "info", {"event": "apply.form_filled", "n": 3})
    assert not any("apply.form_filled" in r.message for r in caplog.records)


def test_unknown_apply_event_warns_once(caplog):
    with caplog.at_level(logging.WARNING, logger="apply.logging"):
        lint_event_name(None, "info", {"event": "apply.foo"})
        lint_event_name(None, "info", {"event": "apply.foo"})
    hits = [r for r in caplog.records if "apply.foo" in r.getMessage()]
    assert len(hits) == 1, f"expected 1 warning, got {len(hits)}"


def test_non_apply_event_not_linted(caplog):
    with caplog.at_level(logging.WARNING, logger="apply.logging"):
        lint_event_name(None, "info", {"event": "gmail.oauth"})
        lint_event_name(None, "info", {"event": "qa.check"})
    assert not caplog.records


def test_unknown_apply_event_still_emits():
    """Criterion 7: the offending event still emits (processor returns dict)."""
    ev = {"event": "apply.unknown_thing", "n": 1}
    out = lint_event_name(None, "info", ev)
    assert out is ev  # linter is a passthrough
    assert out["event"] == "apply.unknown_thing"


def test_allowlist_contains_expected_names():
    """Allowlist now has 12 original + 7 S17-added entries (19 total).

    S17 seam-wiring extended the allowlist with its own event names per
    spec AC#13 (`apply.seam.*`, `apply.review.poll_*`,
    `apply.retention.*`). If the count drifts, this test is the guard.
    """
    assert len(_ALLOWED_EVENT_NAMES) == 19
    # Original S16 12
    for name in [
        "apply.form_navigated",
        "apply.form_filled",
        "apply.captcha_detected",
        "apply.submitted",
        "apply.review_required",
        "apply.failed",
        "apply.dedup_hit",
        "apply.rate_limited",
        "apply.review.auto_declined",
        "apply.field.absent",
        "apply.dry_run.holding_at_submit",
        "apply.session_expired",
    ]:
        assert name in _ALLOWED_EVENT_NAMES
    # S17-added 7
    for name in [
        "apply.seam.enabled",
        "apply.seam.disabled",
        "apply.seam.error",
        "apply.review.poll_started",
        "apply.review.poll_failed",
        "apply.retention.error",
        "apply.retention.rotated",
    ]:
        assert name in _ALLOWED_EVENT_NAMES


# --- 10/9. regex compiled once + no utcnow ----------------------------------


def test_regex_compiled_once():
    assert isinstance(_PII_KEY_RE, re.Pattern)
    # Two invocations do not swap the pattern object.
    r1 = apply_logging._PII_KEY_RE
    scrub_kv(None, "info", {"event": "e", "email": "a@x.com"})
    r2 = apply_logging._PII_KEY_RE
    assert r1 is r2


def test_no_utcnow_in_source():
    src = (ROOT / "src" / "apply" / "logging.py").read_text()
    assert "utcnow" not in src, "L6: datetime.utcnow forbidden"


# --- 11. pure functional (no input mutation) --------------------------------


def test_processor_returns_new_dict_not_mutation():
    inp = {"event": "evt", "email": "x@x.com", "n": 1}
    snapshot = dict(inp)
    _ = scrub_kv(None, "info", inp)
    assert inp == snapshot, "input dict must not be mutated"


def test_processor_returns_new_dict_not_mutation_nested():
    inner = {"email": "y@x.com", "company": "Acme"}
    inp = {"event": "evt", "filled_fields": inner}
    inner_snapshot = dict(inner)
    _ = scrub_kv(None, "info", inp)
    assert inner == inner_snapshot, "nested dict must not be mutated"


# --- 8. defence-in-depth: non-apply loggers still function ------------------


def test_install_does_not_break_non_apply_loggers():
    """After install, a non-apply logger still emits without exception."""
    install_scrubber()
    non_apply = structlog.get_logger("gmail.client")
    # Should not raise. structlog uses the global processor chain.
    non_apply.info("gmail.oauth", user_email="jane@example.com")


def test_install_scrubs_globally():
    """Install runs the scrubber over any bound logger's event, per criterion 8."""
    buf = io.StringIO()
    install_scrubber()
    # Replace the final renderer with one that writes to our buffer for assertion.
    procs = list(structlog.get_config()["processors"])
    # Keep our processors intact, swap only the LAST renderer for a JSON one so
    # we can grep the output.
    procs[-1] = structlog.processors.JSONRenderer()
    structlog.configure(
        processors=procs,
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )
    log = structlog.get_logger()
    log.info("apply.form_filled", email="secret@example.com", company="Acme")
    output = buf.getvalue()
    assert "secret@example.com" not in output, f"PII leaked into output: {output!r}"
    assert "***REDACTED***" in output
    assert "Acme" in output


# --- parent-directed extra scrutiny: realistic-shape PII --------------------


def test_realistic_pii_shapes_all_redacted():
    """
    Bind real-shape PII values (not placeholder 'x'). Reports which keys the
    substring rule redacts vs. which slip through — surfaces the full_name /
    resume_text gap explicitly.
    """
    inp = {
        "event": "apply.form_filled",
        "email": "jane.doe+work@example.com",
        "phone": "+1 (555) 010-0100",
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "resume_text": "John Doe - Software Engineer at Acme...",
    }
    out = scrub_kv(None, "info", inp)

    # Keys whose substring hits the regex — MUST redact.
    assert out["email"] == _REDACTED
    assert out["phone"] == _REDACTED
    assert out["first_name"] == _REDACTED
    assert out["last_name"] == _REDACTED
    assert out["linkedin_url"] == _REDACTED  # "linkedin" substring hits

    # Documented boundary: these keys do NOT contain any of the 10 substrings.
    # Redacting them would require extending the regex or a caller-boundary
    # rule (adapters must not log these keys).
    assert out["full_name"] == "Jane Doe", (
        "full_name is NOT redacted — the substring `name` is not in the regex. "
        "Caller-boundary: adapters must never log `full_name`. See S16 report."
    )
    assert out["resume_text"] == "John Doe - Software Engineer at Acme...", (
        "resume_text is NOT redacted — no substring hit. Caller-boundary applies."
    )


def test_realistic_pii_shapes_via_nested_filled_fields():
    """
    Same real-shape values, but wrapped in the classic `filled_fields=dict`
    shape adapters emit (variation-B finding #10). One-level recursion redacts.
    """
    inp = {
        "event": "apply.form_filled",
        "filled_fields": {
            "email": "jane.doe+work@example.com",
            "phone": "+1 (555) 010-0100",
            "first_name": "Jane",
            "last_name": "Doe",
            "linkedin": "https://linkedin.com/in/janedoe",
            "company": "Acme",
        },
    }
    out = scrub_kv(None, "info", inp)
    ff = out["filled_fields"]
    assert ff["email"] == _REDACTED
    assert ff["phone"] == _REDACTED
    assert ff["first_name"] == _REDACTED
    assert ff["last_name"] == _REDACTED
    assert ff["linkedin"] == _REDACTED
    assert ff["company"] == "Acme"


# --- module constants sanity -----------------------------------------------


def test_redacted_constant_is_literal_string():
    """Criterion 3: value is the literal '***REDACTED***', not None/empty/truncated."""
    assert _REDACTED == "***REDACTED***"
