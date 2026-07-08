"""
tests/apply/test_retries.py — Tests for the shared @navigation_retry decorator.

Spec: .agent/one-big-feature/auto-apply-2026-07-06/03-specs/11-s11-retries-wrapper.md

Covers:
  - Retry semantics (which exceptions trigger, how many attempts, reraise vs RetryError)
  - functools.wraps preservation
  - Instance-method compatibility
  - `apply.retry` structlog event key hygiene (L7: no PII, args, kwargs, return_value)
  - `@submit_no_retry` marker no-op behavior
  - Timing constraint (< 100ms with patched sleep)
  - Gmail retrofit: `_retry_call` gone, `@navigation_retry` on network methods
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from structlog.testing import capture_logs

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apply.retries import (  # noqa: E402
    RETRYABLE_EXCEPTIONS,
    RetryableError,
    navigation_retry,
    submit_no_retry,
)


# ── Sleep patching ────────────────────────────────────────────────
# Tenacity's default sleeper is `tenacity.nap.sleep` which internally calls
# `time.sleep(seconds)`. Patching `time.sleep` at the fixture level keeps the
# retry suite under 100 ms wall-clock without touching tenacity's captured
# defaults (which are bound at import time, so `monkeypatch.setattr` on
# `tenacity.nap.sleep` after import is too late).
@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("tenacity.nap.sleep", lambda _seconds: None)


# ── Constants / shape ─────────────────────────────────────────────


def test_retryable_exceptions_tuple_contract():
    """RETRYABLE_EXCEPTIONS ships the retry-behavior-preserving set.

    DEVIATES from spec §Acceptance 2 (which locked exactly
    ``(PlaywrightTimeoutError, httpx.HTTPError, httpx.TimeoutException,
    ConnectionError, RetryableError)``). Rationale:
      * spec §Acceptance 8/§14 require the Gmail retrofit to be
        behavior-preserving; the pre-retrofit ``_retry_call`` retried
        ``googleapiclient.errors.HttpError`` and
        ``google.auth.exceptions.TransportError`` — Gmail's most common
        transient failure shapes. Excluding them makes every 500/503 a
        hard fail. Reviewer resolves the internal spec conflict at merge.
      * ``httpx.HTTPError`` narrowed to ``httpx.TransportError`` so
        4xx ``HTTPStatusError`` (client errors) is NOT retried
        (findings #1/#4 from the xhigh review).
      * ``httpx.TimeoutException`` (subclass of ``TransportError``) is
        dropped as redundant.

    The tuple is tested by asserting REQUIRED members are present; order
    and length are no longer pinned — future shards can add more types.
    """
    required = {
        PlaywrightTimeoutError,
        httpx.TransportError,
        ConnectionError,
        RetryableError,
    }
    forbidden = {httpx.HTTPStatusError}  # 4xx client errors must NOT retry

    assert required.issubset(set(RETRYABLE_EXCEPTIONS)), (
        f"missing required retryable types: {required - set(RETRYABLE_EXCEPTIONS)}"
    )
    # HTTPStatusError should not be a top-level entry; and since
    # httpx.HTTPError sweeps it in, HTTPError itself should be gone too.
    assert httpx.HTTPError not in RETRYABLE_EXCEPTIONS, (
        "httpx.HTTPError sweeps in HTTPStatusError (4xx) — use httpx.TransportError instead"
    )
    assert forbidden.isdisjoint(RETRYABLE_EXCEPTIONS)

    # If google client libs are installed (they are, per requirements.txt),
    # their transient-error types MUST be in the retry set to preserve
    # pre-retrofit behavior on the Gmail path.
    try:
        from googleapiclient.errors import HttpError as _GAPIHttpError
        assert _GAPIHttpError in RETRYABLE_EXCEPTIONS, (
            "googleapiclient.errors.HttpError must retry (Gmail 5xx path)"
        )
    except ImportError:
        pass
    try:
        from google.auth.exceptions import TransportError as _GAuthTransportError
        assert _GAuthTransportError in RETRYABLE_EXCEPTIONS, (
            "google.auth.exceptions.TransportError must retry (creds.refresh network hiccup)"
        )
    except ImportError:
        pass


def test_httpx_httpstatuserror_does_not_retry():
    """4xx client errors (via response.raise_for_status()) must NOT retry.

    Prevents wasting 3 attempts + ~2-16s backoff on non-recoverable client
    errors (finding #4). httpx.HTTPStatusError is a subclass of
    httpx.HTTPError, so if HTTPError were in RETRYABLE_EXCEPTIONS the 4xx
    would retry. The correct upper bound is httpx.TransportError.
    """
    counter = {"n": 0}

    @navigation_retry
    def fake_400():
        counter["n"] += 1
        raise httpx.HTTPStatusError(
            "400 bad request",
            request=httpx.Request("GET", "http://example.com"),
            response=httpx.Response(400),
        )

    with pytest.raises(httpx.HTTPStatusError):
        fake_400()
    assert counter["n"] == 1, "HTTPStatusError (4xx) must not be retried"


def test_googleapiclient_httperror_retries():
    """Gmail 5xx wraps into googleapiclient.errors.HttpError — retrofit MUST retry it.

    This is the regression the original tuple silently introduced (finding
    #1). Old ``_retry_call`` retried HttpError explicitly; the new decorator
    must too.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        pytest.skip("googleapiclient not installed")
    counter = {"n": 0}

    def _fake_500():
        # HttpError needs a resp-like object and content bytes.
        resp = MagicMock()
        resp.status = 500
        resp.reason = "Backend Error"
        return HttpError(resp=resp, content=b"boom")

    @navigation_retry
    def gmail_call():
        counter["n"] += 1
        if counter["n"] < 3:
            raise _fake_500()
        return "delivered"

    assert gmail_call() == "delivered"
    assert counter["n"] == 3


def test_google_auth_transporterror_retries():
    """google-auth creds.refresh() raises TransportError on network flake — must retry."""
    try:
        from google.auth.exceptions import TransportError
    except ImportError:
        pytest.skip("google-auth not installed")
    counter = {"n": 0}

    @navigation_retry
    def creds_refresh():
        counter["n"] += 1
        if counter["n"] < 3:
            raise TransportError("network unreachable")
        return "refreshed"

    assert creds_refresh() == "refreshed"
    assert counter["n"] == 3


# ── navigation_retry — retry semantics ────────────────────────────


def test_decorator_retries_on_playwright_timeout():
    """PlaywrightTimeoutError should trigger retry; success on 3rd attempt returns value."""
    counter = {"n": 0}

    @navigation_retry
    def fake():
        counter["n"] += 1
        if counter["n"] < 3:
            raise PlaywrightTimeoutError("transient")
        return "ok"

    assert fake() == "ok"
    assert counter["n"] == 3


def test_decorator_reraises_after_three_attempts():
    """After 3 failed attempts, the ORIGINAL exception reraises (not tenacity.RetryError)."""
    counter = {"n": 0}

    @navigation_retry
    def fake():
        counter["n"] += 1
        raise PlaywrightTimeoutError("permanent")

    with pytest.raises(PlaywrightTimeoutError):
        fake()
    assert counter["n"] == 3


def test_decorator_does_not_retry_valueerror():
    """Non-RETRYABLE exceptions propagate immediately — no retry."""
    counter = {"n": 0}

    @navigation_retry
    def fake():
        counter["n"] += 1
        raise ValueError("do not retry")

    with pytest.raises(ValueError):
        fake()
    assert counter["n"] == 1


def test_decorator_retries_on_httpx_error():
    """httpx.HTTPError (or subclasses like ConnectError) should trigger retry."""
    counter = {"n": 0}

    @navigation_retry
    def fake():
        counter["n"] += 1
        if counter["n"] < 3:
            raise httpx.ConnectError("boom")
        return "yes"

    assert fake() == "yes"
    assert counter["n"] == 3


# ── functools.wraps preservation (L12-adjacent) ──────────────────


def test_decorator_preserves_functools_wraps():
    """__name__, __doc__, __wrapped__ must survive the decorator (importable-name stability)."""

    @navigation_retry
    def target_fn():
        """target doc"""
        return 42

    assert target_fn.__name__ == "target_fn"
    assert target_fn.__doc__ == "target doc"
    assert hasattr(target_fn, "__wrapped__")
    assert target_fn.__wrapped__.__name__ == "target_fn"


# ── Log event key hygiene (L7 — no PII) ──────────────────────────


def test_decorator_emits_retry_log_event_per_attempt():
    """On 3-attempt failure, exactly 2 apply.retry events fire (before-sleep for attempt 1 & 2)."""
    counter = {"n": 0}

    @navigation_retry
    def always_fails():
        counter["n"] += 1
        raise PlaywrightTimeoutError("kaboom")

    with capture_logs() as captured:
        with pytest.raises(PlaywrightTimeoutError):
            always_fails()

    retry_events = [e for e in captured if e.get("event") == "apply.retry"]
    assert len(retry_events) == 2, f"expected 2 apply.retry events, got {len(retry_events)}: {captured}"

    expected_keys = {"attempt", "max_attempts", "wait_seconds", "exception_type", "callable_name", "event"}
    forbidden_keys = {"args", "kwargs", "return_value", "email", "value"}

    for evt in retry_events:
        assert expected_keys.issubset(evt.keys()), f"missing keys in {evt}"
        assert forbidden_keys.isdisjoint(evt.keys()), f"forbidden keys leaked in {evt}"
        assert evt["max_attempts"] == 3
        # Fully-qualified type name so playwright.TimeoutError doesn't
        # collide with builtin TimeoutError / asyncio.TimeoutError in
        # observability filters (finding #7).
        assert evt["exception_type"] == (
            f"{PlaywrightTimeoutError.__module__}.{PlaywrightTimeoutError.__qualname__}"
        )
        assert "." in evt["exception_type"], (
            f"exception_type must be fully-qualified module.qualname, got {evt['exception_type']!r}"
        )
        assert evt["callable_name"] == "always_fails"
        # wait_seconds should be a non-negative float
        assert isinstance(evt["wait_seconds"], (int, float))
        assert evt["wait_seconds"] >= 0

    # First event: attempt 1 just failed; second event: attempt 2 just failed.
    assert retry_events[0]["attempt"] == 1
    assert retry_events[1]["attempt"] == 2


# ── Instance method support ───────────────────────────────────────


def test_decorator_works_on_instance_method():
    """Wrapping an instance method must not eat `self` and must retry as usual."""

    class Widget:
        def __init__(self):
            self.tries = 0

        @navigation_retry
        def flakey(self, x):
            self.tries += 1
            if self.tries < 3:
                raise ConnectionError("net gone")
            return x * 2

    w = Widget()
    assert w.flakey(5) == 10
    assert w.tries == 3


# ── submit_no_retry marker ────────────────────────────────────────


def test_submit_no_retry_is_noop_marker():
    """@submit_no_retry must NOT retry; docstring must document the contract."""
    counter = {"n": 0}

    @submit_no_retry
    def submit():
        counter["n"] += 1
        raise PlaywrightTimeoutError("form submit failed")

    with pytest.raises(PlaywrightTimeoutError):
        submit()
    assert counter["n"] == 1, "submit_no_retry must not retry — double-application risk"

    # Contract docstring — S8's reviewer greps for this text
    # (spec §Acceptance 6 pins the exact string; case-preserved).
    assert submit_no_retry.__doc__ is not None
    assert "MARKER: this call must NEVER be retried; propagate exceptions immediately." in submit_no_retry.__doc__


def test_submit_no_retry_preserves_functools_wraps():
    """The marker decorator must preserve the wrapped function's identity."""

    @submit_no_retry
    def my_submit():
        """clicks submit"""
        return "clicked"

    assert my_submit.__name__ == "my_submit"
    assert my_submit.__doc__ == "clicks submit"
    assert hasattr(my_submit, "__wrapped__")


# ── Timing constraint ────────────────────────────────────────────


def test_retries_complete_under_100ms_with_patched_sleep():
    """Full 3-attempt failure sequence must complete in < 100ms with sleep patched."""

    @navigation_retry
    def fail():
        raise PlaywrightTimeoutError("fail")

    start = time.perf_counter()
    with pytest.raises(PlaywrightTimeoutError):
        fail()
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"3-attempt retry took {elapsed:.4f}s — sleep not patched?"


# ── Gmail retrofit — behavior-preserving ──────────────────────────


def _make_gmail_client(fake_service):
    """Test helper: build a GmailClient without triggering OAuth or googleapiclient.build."""
    with patch("gmail.client.GmailClient._authenticate", return_value=MagicMock()), patch(
        "gmail.client.build", return_value=fake_service
    ):
        from gmail.client import GmailClient

        return GmailClient()


def test_gmail_client_retrofit_retries_on_googleapiclient_httperror():
    """
    Post-retrofit, send_email must retry on ``googleapiclient.errors.HttpError``
    — the actual exception googleapiclient raises on 5xx (finding #3).

    The old ``httpx.HTTPError`` in the test injected an exception the real
    Gmail stack never raises, painting a green ceiling over the retry-set
    regression.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        pytest.skip("googleapiclient not installed")

    fake_service = MagicMock()
    client = _make_gmail_client(fake_service)
    # The before_sleep_extra hook will invoke client.refresh_connection between
    # attempts; it hits the real googleapiclient.build() otherwise (which
    # rejects our MagicMock creds with UniverseMismatchError). Stub it — the
    # retry semantics under test are exception-set membership, not the actual
    # refresh mechanics.
    client.refresh_connection = lambda: None

    resp = MagicMock()
    resp.status = 503
    resp.reason = "Backend Error"
    execute_mock = fake_service.users.return_value.messages.return_value.send.return_value.execute
    execute_mock.side_effect = [
        HttpError(resp=resp, content=b"transient 503"),
        HttpError(resp=resp, content=b"transient 503"),
        {"id": "sent-msg"},
    ]

    client.send_email(to="x@example.com", subject="s", body_text="b")
    assert execute_mock.call_count == 3


def test_gmail_client_refreshes_connection_between_retries():
    """
    Fixes finding #2: OAuth token expiry / stale service handle must be
    recoverable mid-retry. Between attempts, ``refresh_connection`` must
    fire so attempt 2/3 sees fresh creds.
    """
    fake_service = MagicMock()
    client = _make_gmail_client(fake_service)

    refresh_calls = {"n": 0}
    original_refresh = client.refresh_connection

    def _spy_refresh():
        refresh_calls["n"] += 1
        # Don't actually re-auth — just record the call.

    client.refresh_connection = _spy_refresh

    execute_mock = fake_service.users.return_value.messages.return_value.send.return_value.execute
    execute_mock.side_effect = [
        ConnectionError("stale socket"),
        ConnectionError("stale socket"),
        {"id": "sent-msg"},
    ]

    client.send_email(to="x@example.com", subject="s", body_text="b")

    # 2 failures → 2 before-sleep hooks → 2 refresh_connection calls
    # between attempts. (No refresh before attempt 1; no refresh after
    # the successful attempt 3.)
    assert refresh_calls["n"] == 2, (
        f"expected 2 mid-retry refreshes, got {refresh_calls['n']}"
    )


def test_gmail_client_retry_call_removed():
    """`_retry_call` must be gone; every network method must carry retry semantics.

    Presence check is TIGHTENED (finding #4-in-review): asserting
    ``__wrapped__`` alone is satisfied by ANY functools.wraps-style
    decorator (including ``@submit_no_retry``). Instead assert the
    tenacity ``Retrying`` instance is attached at ``.retry``.
    """
    from gmail.client import GmailClient

    assert not hasattr(GmailClient, "_retry_call"), "_retry_call still present — retrofit incomplete"

    for method_name in (
        "find_unprocessed_alert",
        "get_unread_alerts",
        "mark_processed",
        "send_email",
    ):
        method = getattr(GmailClient, method_name)
        # tenacity attaches its Retrying instance as `.retry` on the
        # decorated callable — that's the tight identity check for
        # @navigation_retry (submit_no_retry does NOT set `.retry`).
        assert hasattr(method, "retry"), f"{method_name} missing @navigation_retry (no .retry attr)"
        from tenacity import BaseRetrying

        assert isinstance(method.retry, BaseRetrying), (
            f"{method_name}.retry is not a tenacity Retrying instance"
        )


# ── before_sleep hook robustness ─────────────────────────────────


def test_before_sleep_extra_exception_does_not_break_retry():
    """A raising before_sleep_extra hook must not abort the retry loop.

    From the high-effort code-review pass: if the hook propagates an
    exception, tenacity's ``before_sleep`` re-raises it and the retry
    loop terminates — the wrapped call never gets attempts 2/3 and the
    caller sees the hook's exception instead of the original transient
    error. The chained before_sleep must swallow hook faults so the
    retry contract is preserved.
    """
    counter = {"n": 0}

    def bad_hook(_retry_state):
        raise RuntimeError("hook is broken")

    @navigation_retry(before_sleep_extra=bad_hook)
    def flakey():
        counter["n"] += 1
        if counter["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    # Retry must still succeed on attempt 3 — the hook fault is neither
    # observable to the caller nor mistaken for a retryable exception.
    assert flakey() == "ok"
    assert counter["n"] == 3


def test_log_retry_exception_does_not_break_retry(monkeypatch):
    """A raising _log_retry must not abort the retry loop either.

    Symmetric guard: structlog processor misconfiguration or a closed
    stdout must not silently disable retries + before_sleep_extra hooks.
    """
    from apply import retries as _retries_mod

    def _boom(_retry_state):
        raise RuntimeError("logging broken")

    monkeypatch.setattr(_retries_mod, "_log_retry", _boom)

    hook_calls = {"n": 0}

    def _hook(_retry_state):
        hook_calls["n"] += 1

    counter = {"n": 0}

    @navigation_retry(before_sleep_extra=_hook)
    def flakey():
        counter["n"] += 1
        if counter["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    # Even with logging broken, the before_sleep_extra hook and the
    # retry loop must still work.
    assert flakey() == "ok"
    assert counter["n"] == 3
    assert hook_calls["n"] == 2, "extra hook must still fire despite logging failure"


# ── No hardcoded ATS names (L14) ─────────────────────────────────


def test_module_has_no_hardcoded_ats_names():
    """This shard is ATS-agnostic — no adapter names embedded (L14)."""
    module_path = ROOT / "src" / "apply" / "retries.py"
    source = module_path.read_text().lower()
    for name in ("greenhouse", "ashby", "lever", "workday", "smartrecruiters"):
        assert name not in source, f"hardcoded ATS name '{name}' in retries.py (L14)"


# ── No deprecated datetime.utcnow (L6) ───────────────────────────


def test_module_does_not_use_deprecated_utcnow():
    """L6: no datetime.utcnow() — deprecated in Python 3.12+.

    Checks for actual call syntax `datetime.utcnow(` — the substring may
    appear in the module docstring documenting the L6 anti-pattern; we only
    care about actual invocations.
    """
    module_path = ROOT / "src" / "apply" / "retries.py"
    source = module_path.read_text()
    assert "datetime.utcnow(" not in source, "L6: datetime.utcnow() is deprecated"
