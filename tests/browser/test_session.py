"""
tests/browser/test_session.py — RED tests for the S4 browser-session shard.

Every test here maps to one acceptance criterion in
.agent/one-big-feature/auto-apply-2026-07-06/03-specs/04-s4-browser-session.md.

Landmine regression coverage:
  - L5: browser leak on setup failure (test_session_closes_browser_when_context_creation_fails)
  - L6: datetime.utcnow (test_session_uses_tz_aware_datetime)
  - L7: no URL / storage_state contents in log records (test_session_never_logs_url_or_state_contents)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src/ importable regardless of how pytest is invoked.
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from browser.session import session  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


class _LogCapture:
    """
    Minimal log-capture: records every structlog event as
    (event_name, kwargs_dict) tuples. structlog's `contextvars=None`
    kwargs pass through in the processor chain via `event_dict`.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def __call__(self, logger, method_name, event_dict):
        # Copy so downstream mutations do not affect our capture.
        self.records.append((event_dict.get("event", ""), dict(event_dict)))
        # Drop the record so structlog does not try to render it.
        raise structlog.DropEvent


import structlog  # noqa: E402  (imported after helper defs to avoid circulars in test file)


@pytest.fixture
def log_capture(monkeypatch):
    """Install a structlog processor that captures every log call from browser.session."""
    cap = _LogCapture()
    # Reconfigure structlog for this test only; restore in teardown.
    original = structlog.get_config()
    structlog.configure(
        processors=[cap],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    )
    yield cap
    structlog.configure(**original)


# ── Acceptance-criterion tests ────────────────────────────────────────────────


def test_session_yields_page_and_none_trace_when_no_trace_dir(tmp_path):
    """AC #2: session yields (page, None) when trace_dir is not passed."""
    with session() as (page, trace_path):
        assert trace_path is None
        # Basic sanity: page is a Playwright Page-like object.
        assert hasattr(page, "goto")


def test_session_writes_trace_zip_and_permissions(tmp_path):
    """
    AC #4, #12.3: trace file exists, is non-empty, mode 0o600;
    trace_dir mode is 0o700.
    """
    trace_dir = tmp_path / "trace"
    with session(trace_dir=trace_dir) as (page, trace_path):
        page.goto("about:blank")
    assert trace_path is not None
    assert trace_path.exists()
    assert trace_path.stat().st_size > 0
    assert oct(trace_path.stat().st_mode)[-3:] == "600"
    assert oct(trace_dir.stat().st_mode)[-3:] == "700"
    assert trace_path.suffix == ".zip"
    # UUID stem — no timestamp — spec §Interface `<uuid>.zip`.
    assert len(trace_path.stem) >= 32  # uuid string length (with hyphens)


def test_session_hydrates_storage_state_when_file_exists(tmp_path):
    """
    AC #6: if storage_state_path exists, it is fed to browser.new_context;
    cookies from that state should be visible in page.context.cookies().
    """
    state_file = tmp_path / "state.json"
    state = {
        "cookies": [
            {
                "name": "sess",
                "value": "hydrated-token-abc123",
                "domain": "example.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    state_file.write_text(json.dumps(state))
    with session(storage_state_path=state_file) as (page, _):
        cookies = page.context.cookies()
        names = [c["name"] for c in cookies]
        assert "sess" in names
        for c in cookies:
            if c["name"] == "sess":
                assert c["value"] == "hydrated-token-abc123"


def test_session_creates_storage_state_on_first_run(tmp_path):
    """
    AC #5, #6: if storage_state_path does NOT exist, no error is raised;
    on clean exit the file is written and mode is 0o600.
    """
    state_file = tmp_path / "sub" / "state.json"
    assert not state_file.exists()
    with session(storage_state_path=state_file) as (page, _):
        pass  # empty session — file should still be written on exit
    assert state_file.exists()
    assert oct(state_file.stat().st_mode)[-3:] == "600"
    assert oct(state_file.parent.stat().st_mode)[-3:] == "700"


def test_session_closes_browser_when_context_creation_fails():
    """
    AC #3, L5: browser.new_context raising must still result in browser.close()
    being called (Chromium leak proof).
    """
    fake_browser = MagicMock(name="fake_browser")
    fake_browser.new_context.side_effect = RuntimeError("kaboom in new_context")

    fake_chromium = MagicMock(name="fake_chromium")
    fake_chromium.launch.return_value = fake_browser

    fake_pw = MagicMock(name="fake_pw")
    fake_pw.chromium = fake_chromium

    with patch("browser.session.sync_playwright") as spw:
        spw.return_value.start.return_value = fake_pw
        with pytest.raises(RuntimeError, match="kaboom"):
            with session() as (_page, _trace):
                pytest.fail("should not enter body — context creation failed")

    fake_browser.close.assert_called_once()
    fake_pw.stop.assert_called_once()


def test_session_closes_cleanly_on_body_exception(tmp_path):
    """
    AC #7: exception raised inside the with-body must propagate AND all
    close methods must run. storage_state is still written on the way out.
    """
    state_file = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="body-error"):
        with session(storage_state_path=state_file) as (page, _):
            page.goto("about:blank")
            raise RuntimeError("body-error")
    # storage_state was written despite the exception
    assert state_file.exists()
    assert oct(state_file.stat().st_mode)[-3:] == "600"


def test_session_never_logs_url_or_state_contents(tmp_path, log_capture):
    """
    AC #10, L7 regression: no log record's event OR kwargs may contain
    URLs, cookie values, or user_agent strings.
    """
    state_file = tmp_path / "state.json"
    state = {
        "cookies": [
            {
                "name": "sess",
                "value": "supersecret-token-XYZ",
                "domain": "example.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    state_file.write_text(json.dumps(state))
    with session(storage_state_path=state_file) as (page, _):
        page.goto("about:blank")

    forbidden = ["about:blank", "supersecret-token-XYZ", "Chrome/122.0", "Mozilla/5.0"]
    for event, kwargs in log_capture.records:
        blob = event + " " + " ".join(f"{k}={v}" for k, v in kwargs.items())
        for needle in forbidden:
            assert needle not in blob, (
                f"log record leaks '{needle}': event={event!r} kwargs={kwargs!r}"
            )


def test_session_uses_tz_aware_datetime():
    """L6 regression: no datetime.utcnow( in the shard source."""
    src = (ROOT / "src" / "browser" / "session.py").read_text()
    assert "datetime.utcnow(" not in src, (
        "session.py must not use datetime.utcnow() — use datetime.now(timezone.utc)"
    )


def test_playwright_started_exactly_once(tmp_path):
    """
    AC #8: sync_playwright().start() is called exactly once per session()
    context, and .stop() is called exactly once.
    """
    # Spy on the real playwright bindings by wrapping the module attribute.
    # NB: `from browser import session` returns the function; use importlib
    # to get the actual submodule (name shadowing in browser/__init__.py).
    import importlib

    session_mod = importlib.import_module("browser.session")

    real_sync_playwright = session_mod.sync_playwright
    start_calls = {"n": 0}
    stop_calls = {"n": 0}

    class _WrappedManager:
        def __init__(self, inner):
            self._inner = inner

        def start(self):
            start_calls["n"] += 1
            pw = self._inner.start()
            real_stop = pw.stop

            def wrapped_stop():
                stop_calls["n"] += 1
                return real_stop()

            pw.stop = wrapped_stop
            return pw

    def wrapped():
        return _WrappedManager(real_sync_playwright())

    with patch.object(session_mod, "sync_playwright", wrapped):
        with session() as (page, _):
            pass

    assert start_calls["n"] == 1, f"start called {start_calls['n']} times, expected 1"
    assert stop_calls["n"] == 1, f"stop called {stop_calls['n']} times, expected 1"


def test_session_signature_matches_spec():
    """AC #2: signature is exactly the frozen interface."""
    import inspect
    from browser.session import session as sess

    sig = inspect.signature(sess.__wrapped__ if hasattr(sess, "__wrapped__") else sess)
    # If contextmanager wraps, __wrapped__ carries the underlying func.
    if hasattr(sess, "__wrapped__"):
        sig = inspect.signature(sess.__wrapped__)
    params = sig.parameters
    assert set(params.keys()) == {
        "headless",
        "storage_state_path",
        "user_agent",
        "viewport",
        "trace_dir",
    }
    # All keyword-only.
    for name, p in params.items():
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
    # Defaults per spec.
    assert params["headless"].default is True
    assert params["storage_state_path"].default is None
    assert params["user_agent"].default is None
    assert params["viewport"].default is None
    assert params["trace_dir"].default is None
