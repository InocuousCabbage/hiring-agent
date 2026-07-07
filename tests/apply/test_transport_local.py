"""
tests/apply/test_transport_local.py — RED tests for LocalTransport (S10).

Every test maps to an acceptance criterion in
.agent/one-big-feature/auto-apply-2026-07-06/03-specs/10-s10-browserbase-transport.md.

Focus:
- AC #3: LocalTransport delegates to S4's `browser.session()` and yields a
  TransportSession with transport="local", replay_url=None, proxies_enabled=False,
  solve_captchas=False.
- AC #5 shape: nested try/finally still runs the underlying session __exit__
  when the caller raises inside the `with` block.

Testing seams:
- LocalTransport calls `browser.session(...)` via a lazy import so tests can
  install a fake `browser` module into `sys.modules` without S4 present.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeSessionCM:
    """
    Stand-in for the ctx-manager returned by S4's `browser.session()`.
    Records enter/exit so tests can assert the outer try/finally still tears down.
    """

    def __init__(self, page):
        self._page = page
        self.entered = False
        self.exited = False
        self.exit_exc_type = None

    def __enter__(self):
        self.entered = True
        return (self._page, None)  # (page, trace_path_or_None)

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        self.exit_exc_type = exc_type
        return False  # never suppress


def _install_fake_browser(monkeypatch, fake_cm, captured_kwargs: dict):
    """
    Inject a fake `browser` module into sys.modules so LocalTransport's
    lazy `import browser; browser.session(...)` resolves to our fake.
    """
    fake_mod = types.ModuleType("browser")

    def _fake_session(**kwargs):
        captured_kwargs.update(kwargs)
        return fake_cm

    fake_mod.session = _fake_session
    monkeypatch.setitem(sys.modules, "browser", fake_mod)
    return fake_mod


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_local_transport_delegates_to_session(monkeypatch):
    """AC #3: LocalTransport.open delegates to browser.session and yields TransportSession."""
    from apply.transport import LocalTransport, TransportSession

    fake_page = MagicMock(name="fake_page")
    fake_cm = _FakeSessionCM(fake_page)
    captured: dict = {}
    _install_fake_browser(monkeypatch, fake_cm, captured)

    transport = LocalTransport()
    with transport.open("https://boards.greenhouse.io/example/jobs/1", None) as ts:
        assert isinstance(ts, TransportSession)
        assert ts.page is fake_page
        assert ts.transport == "local"
        assert ts.replay_url is None
        assert ts.proxies_enabled is False
        assert ts.solve_captchas is False

    # AC #3: goto called with url and session opened with headless=True.
    fake_page.goto.assert_called_once_with("https://boards.greenhouse.io/example/jobs/1")
    assert captured.get("headless") is True
    assert fake_cm.entered is True
    assert fake_cm.exited is True


def test_local_transport_closes_underlying_session_on_exception(monkeypatch):
    """AC #5 shape: exception inside `with` still runs underlying session __exit__."""
    from apply.transport import LocalTransport

    fake_page = MagicMock(name="fake_page")
    fake_cm = _FakeSessionCM(fake_page)
    _install_fake_browser(monkeypatch, fake_cm, {})

    transport = LocalTransport()
    with pytest.raises(RuntimeError, match="boom"):
        with transport.open("https://x", None):
            raise RuntimeError("boom")

    assert fake_cm.exited is True
    assert fake_cm.exit_exc_type is RuntimeError


def test_local_transport_ignores_dict_storage_state_without_error(monkeypatch):
    """LocalTransport accepts (but does not require) a storage_state dict; S17 will
    materialize state for real. This test guarantees the Protocol shape works today
    without crashing on a passed dict."""
    from apply.transport import LocalTransport

    fake_page = MagicMock(name="fake_page")
    fake_cm = _FakeSessionCM(fake_page)
    captured: dict = {}
    _install_fake_browser(monkeypatch, fake_cm, captured)

    storage_state = {"cookies": [{"name": "sess", "value": "x", "domain": ".example.com", "path": "/"}]}

    transport = LocalTransport()
    with transport.open("https://x", storage_state) as ts:
        assert ts.transport == "local"
    # LocalTransport must not put cookie values into session() kwargs.
    kwargs_str = repr(captured)
    assert "sess" not in kwargs_str or "cookies" not in kwargs_str
