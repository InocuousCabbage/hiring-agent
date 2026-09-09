"""
tests/test_email_parser_docs.py — cheap guard against the email_parser docs
re-drifting back to the stale /viewjob assumption (T4).

The module docstring used to describe SendGrid links as redirecting to
`hiring.cafe/viewjob/{job_id}` — a shape that is now 410 Gone. The current
target is `hiringcafe.com/job/{slug}-{id}` with fields in #__NEXT_DATA__.
resolve_sendgrid_url itself is format-agnostic (no logic change), so this is a
documentation guard, not a behavior test.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import parser.email_parser as email_parser  # noqa: E402


def test_module_docstring_describes_current_job_shape():
    doc = email_parser.__doc__ or ""
    assert "hiringcafe.com/job/" in doc
    # /viewjob must be present only as the *legacy* 410 target, never presented
    # as the current redirect destination.
    assert "410" in doc


def test_resolve_sendgrid_url_docstring_notes_format_agnostic():
    doc = email_parser.resolve_sendgrid_url.__doc__ or ""
    assert "job/{slug}-{id}" in doc
    assert "410" in doc
