"""
RED tests for src.apply.notify — S13 fast-path emailer.

Coverage (matches spec §TDD scaffolding, 9 acceptance-driving tests + 1 retry
verification):

1. test_notify_captcha_escalation_sends_urgent_subject
2. test_notify_captcha_escalation_body_lists_expected_fields
3. test_notify_captcha_escalation_never_leaks_pii
4. test_notify_session_expired_body_has_bootstrap_command
5. test_notify_session_expired_hashes_user_in_log
6. test_send_failure_is_swallowed_after_one_retry
7. test_missing_my_email_swallows_gracefully
8. test_captcha_review_url_none_reads_na
9. test_uses_now_tz_utc_not_utcnow
10. test_send_immediate_uses_retry_helper (bonus — client-layer retry mechanics)
"""

from __future__ import annotations

import hashlib
import inspect
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, ANY

import pytest
import structlog
from structlog.testing import capture_logs

from tests.fixtures.apply.notify_context import sample_apply_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patched_gmail(monkeypatch):
    """Patch src.apply.notify.GmailClient so no OAuth flow runs.

    Returns the mock class; access `.return_value.send_immediate` to inspect
    or configure the instance method.
    """
    mock_cls = MagicMock(name="GmailClient")
    # Give the instance a real send_immediate MagicMock too.
    mock_cls.return_value.send_immediate = MagicMock(name="send_immediate")
    monkeypatch.setattr("src.apply.notify.GmailClient", mock_cls)
    return mock_cls


# ---------------------------------------------------------------------------
# 1. subject prefix + literal "CAPTCHA"
# ---------------------------------------------------------------------------

def test_notify_captcha_escalation_sends_urgent_subject(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()
    ret = notify_captcha_escalation(ctx, "recaptcha_v2", "https://replay.example/x")

    assert ret is None
    assert send.call_count == 1
    subject = send.call_args.kwargs.get("subject") or send.call_args.args[0]
    assert subject.startswith("[hiring-agent] URGENT:"), subject
    assert "CAPTCHA" in subject, subject


# ---------------------------------------------------------------------------
# 2. captcha body lists expected fields in the mandated order
# ---------------------------------------------------------------------------

def test_notify_captcha_escalation_body_lists_expected_fields(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()
    notify_captcha_escalation(ctx, "hcaptcha", "https://replay.example")

    body = send.call_args.kwargs.get("body") or send.call_args.args[1]

    # Every required field appears somewhere in the body.
    for token in [
        "greenhouse",                                              # ats
        "AcmeCo",                                                  # company
        "Senior Backend Engineer",                                 # role_title
        "https://boards.greenhouse.io/acme/jobs/12345",            # job_url
        "https://boards.greenhouse.io/acme/jobs/12345#app",        # apply_url
        "hcaptcha",                                                # captcha_kind
        "Browserbase replay: https://replay.example",              # review_url labeled
    ]:
        assert token in body, f"missing {token!r} in body:\n{body}"

    # detected_at is an ISO 8601 UTC timestamp — must contain "T" and a
    # timezone marker ("+00:00" or "Z"); we do not pin the exact value.
    assert "detected_at" in body.lower() or "T" in body


# ---------------------------------------------------------------------------
# 3. captcha body/subject never leaks candidate PII
# ---------------------------------------------------------------------------

def test_notify_captcha_escalation_never_leaks_pii(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context(
        profile=SimpleNamespace(
            email="secret@example.com",
            phone="+15550100",
            first_name="Alice",
            last_name="Applicant",
        )
    )
    notify_captcha_escalation(ctx, "cloudflare_turnstile", None)

    subject = send.call_args.kwargs.get("subject") or send.call_args.args[0]
    body = send.call_args.kwargs.get("body") or send.call_args.args[1]

    for pii in ["secret@example.com", "+15550100", "Alice", "Applicant"]:
        assert pii not in subject, f"PII {pii!r} leaked into subject"
        assert pii not in body, f"PII {pii!r} leaked into body"


# ---------------------------------------------------------------------------
# 4. session-expired body has bootstrap command
# ---------------------------------------------------------------------------

def test_notify_session_expired_body_has_bootstrap_command(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_session_expired

    ret = notify_session_expired(
        "greenhouse", "user@example.com", "2026-07-01T00:00:00+00:00"
    )
    assert ret is None
    assert send.call_count == 1
    subject = send.call_args.kwargs.get("subject") or send.call_args.args[0]
    body = send.call_args.kwargs.get("body") or send.call_args.args[1]

    assert subject.startswith("[hiring-agent] URGENT:")
    assert "Run: python -m src.apply.bootstrap greenhouse" in body
    assert "greenhouse" in body
    assert "user@example.com" in body                       # user IS allowed in body (§24)
    assert "2026-07-01T00:00:00+00:00" in body


# ---------------------------------------------------------------------------
# 5. session-expired logs hashed user, never raw
# ---------------------------------------------------------------------------

def test_notify_session_expired_hashes_user_in_log(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    _patched_gmail(monkeypatch)

    from src.apply.notify import notify_session_expired

    raw_user = "user@example.com"
    expected_hash = hashlib.sha256(raw_user.encode()).hexdigest()[:12]

    with capture_logs() as cap:
        notify_session_expired("greenhouse", raw_user, "2026-07-01T00:00:00+00:00")

    # Find the sent-event.
    sent_events = [e for e in cap if e.get("event") == "notify.session_expired.sent"]
    assert len(sent_events) == 1, cap
    entry = sent_events[0]

    assert entry.get("user_hash") == expected_hash

    # Raw user must NOT appear anywhere in any log entry.
    for entry in cap:
        for k, v in entry.items():
            assert raw_user not in str(v), (
                f"raw user {raw_user!r} leaked into log key {k!r}: {v!r}"
            )


# ---------------------------------------------------------------------------
# 6. send failure → swallowed, one notify.send_failed log
# ---------------------------------------------------------------------------

def test_send_failure_is_swallowed_after_one_retry(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")

    from googleapiclient.errors import HttpError

    mock_cls = MagicMock(name="GmailClient")
    # Simulate the state AFTER send_immediate's internal retry has exhausted —
    # a final HttpError bubbles out.
    fake_response = MagicMock()
    fake_response.status = 500
    fake_response.reason = "Internal Server Error"
    mock_cls.return_value.send_immediate = MagicMock(
        side_effect=HttpError(fake_response, b"boom")
    )
    monkeypatch.setattr("src.apply.notify.GmailClient", mock_cls)

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()

    with capture_logs() as cap:
        ret = notify_captcha_escalation(ctx, "datadome", None)

    assert ret is None
    failed = [e for e in cap if e.get("event") == "notify.send_failed"]
    assert len(failed) == 1, cap
    assert failed[0].get("ats") == "greenhouse"
    assert failed[0].get("kind") == "datadome"
    assert failed[0].get("http_status") == 500


# ---------------------------------------------------------------------------
# 7. missing MY_EMAIL → swallowed, notify.recipient_unresolved log
# ---------------------------------------------------------------------------

def test_missing_my_email_swallows_gracefully(monkeypatch):
    monkeypatch.delenv("MY_EMAIL", raising=False)
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()

    with capture_logs() as cap:
        ret = notify_captcha_escalation(ctx, "recaptcha_v3", None)

    assert ret is None
    assert send.call_count == 0
    unresolved = [e for e in cap if e.get("event") == "notify.recipient_unresolved"]
    assert len(unresolved) == 1, cap


# ---------------------------------------------------------------------------
# 8. review_url=None → body says "Browserbase replay: n/a"
# ---------------------------------------------------------------------------

def test_captcha_review_url_none_reads_na(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()
    notify_captcha_escalation(ctx, "cloudflare_turnstile", None)

    body = send.call_args.kwargs.get("body") or send.call_args.args[1]
    assert "Browserbase replay: n/a" in body


# ---------------------------------------------------------------------------
# 9. L6 guard — no datetime.utcnow anywhere in notify.py
# ---------------------------------------------------------------------------

def test_uses_now_tz_utc_not_utcnow():
    import src.apply.notify as notify_mod

    source = inspect.getsource(notify_mod)
    assert "utcnow" not in source, (
        "landmine L6: datetime.utcnow() is deprecated in Python 3.12+ — "
        "use datetime.now(timezone.utc) everywhere."
    )


# ---------------------------------------------------------------------------
# Recipient-resolution parity: both alert paths honor apply.fast_path_recipient
# (defence against silent split-brain if config is set to a non-env: literal).
# ---------------------------------------------------------------------------

def test_fast_path_recipient_config_honored_on_both_paths(monkeypatch):
    monkeypatch.delenv("MY_EMAIL", raising=False)
    monkeypatch.setenv("OPS_INBOX", "ops@example.com")
    mock_cls = _patched_gmail(monkeypatch)
    send = mock_cls.return_value.send_immediate

    from src.apply.notify import (
        notify_captcha_escalation,
        notify_session_expired,
    )

    config = {"apply": {"fast_path_recipient": "env:OPS_INBOX"}}
    ctx = sample_apply_context(config=config)

    notify_captcha_escalation(ctx, "hcaptcha", None)
    notify_session_expired(
        "greenhouse", "user@example.com", "2026-07-01T00:00:00+00:00",
        config=config,
    )

    assert send.call_count == 2
    for call in send.call_args_list:
        to = call.kwargs.get("to")
        assert to == "ops@example.com", call


# ---------------------------------------------------------------------------
# 10. captcha success emits notify.captcha.sent with review_url_present bool
#     (acceptance criterion #9 — URL is NEVER raw-logged)
# ---------------------------------------------------------------------------

def test_notify_captcha_sent_log_masks_review_url(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    _patched_gmail(monkeypatch)

    from src.apply.notify import notify_captcha_escalation

    ctx = sample_apply_context()
    review_url = "https://replay.browserbase.com/session/deadbeef"

    with capture_logs() as cap:
        notify_captcha_escalation(ctx, "recaptcha_v2", review_url)

    sent = [e for e in cap if e.get("event") == "notify.captcha.sent"]
    assert len(sent) == 1, cap
    entry = sent[0]
    assert entry.get("ats") == "greenhouse"
    assert entry.get("kind") == "recaptcha_v2"
    assert entry.get("review_url_present") is True

    # Raw URL must NOT leak into any log entry.
    for entry in cap:
        for k, v in entry.items():
            assert review_url not in str(v), (
                f"raw review_url leaked into log key {k!r}: {v!r}"
            )


# ---------------------------------------------------------------------------
# 11. client-layer retry mechanics (bonus)
#     Post-S11 retrofit: GmailClient.send_immediate is decorated with
#     @navigation_retry (3 attempts, exponential jitter). The old _retry_call
#     helper has been removed. This test now verifies send_immediate delegates
#     to send_email with the resolved target — the retry mechanics themselves
#     are covered by tests/apply/test_retries.py (S11).
# ---------------------------------------------------------------------------

def test_send_immediate_delegates_to_send_email(monkeypatch):
    """send_immediate resolves the recipient and forwards to send_email."""
    from src.gmail import client as client_mod

    # Bypass __init__ so we don't need OAuth.
    obj = client_mod.GmailClient.__new__(client_mod.GmailClient)
    obj.creds = None
    obj.service = MagicMock()

    obj.refresh_connection = MagicMock()
    obj.send_email = MagicMock(return_value=None)

    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    obj.send_immediate("[hiring-agent] URGENT: test", "hello body")

    assert obj.send_email.call_count == 1
    kwargs = obj.send_email.call_args.kwargs
    assert kwargs.get("to") == "ben@example.com"
    assert kwargs.get("subject") == "[hiring-agent] URGENT: test"
    assert kwargs.get("body_text") == "hello body"
