"""H4: dispatcher calls adapter.apply(page=None, ctx=ctx) — no transport wired.

Nothing in the wired path invokes get_transport() or opens a session. Transport
modules (browserbase.py, local.py) are DEAD CODE from the wired path. Every
real apply attempt soft-fails because adapters get page=None.

Fix: dispatcher opens a session via get_transport() before adapter.apply(),
passes the yielded page, and owns the session context lifecycle.
"""
from __future__ import annotations

import sys
import types as pytypes
from pathlib import Path

import pytest


class _RealPage:
    """A recognizable stand-in for a Playwright Page."""

    def __init__(self, marker: str = "real-page"):
        self.marker = marker
        self.url = "https://boards.greenhouse.io/example/jobs/12345"

    def close(self):  # pragma: no cover — defensive
        pass


class _AdapterThatRecordsPage:
    """Adapter that records the `page` object it was handed."""

    name = "greenhouse"
    domains = ("boards.greenhouse.io",)
    _received_page = None

    def detect(self, url: str) -> bool:
        return any(d in url for d in self.domains)

    def apply(self, page, ctx):  # noqa: ARG002
        type(self)._received_page = page
        from src.apply.types import ApplyResult

        return ApplyResult(status="submitted", ats="greenhouse")


class _FakeTransport:
    """Test transport whose open() context manager yields a _RealPage."""

    from contextlib import contextmanager

    call_count = 0

    def __init__(self):
        pass

    @classmethod
    def reset(cls):
        cls.call_count = 0
        cls.last_url = None

    @contextmanager
    def open(self, url, storage_state):
        type(self).call_count += 1
        type(self).last_url = url

        class _FakeSession:
            page = _RealPage()
            replay_url = None
            transport = "local"
            proxies_enabled = False
            solve_captchas = False

        yield _FakeSession()


def _install_fake_adapter(monkeypatch, adapter_cls):
    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []
    greenhouse_mod = pytypes.ModuleType("src.apply.adapters.greenhouse")
    greenhouse_mod.GreenhouseAdapter = adapter_cls
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", greenhouse_mod)


def _sample_ctx():
    from src.apply.types import ApplyContext
    from tests.fixtures.apply.profile_factory import load_example_profile

    return ApplyContext(
        profile=load_example_profile(),
        job={"url": "https://boards.greenhouse.io/example/jobs/12345"},
        resume_path=Path("/tmp/resume.pdf"),
        cover_letter_path=None,
        config={"apply": {"allowed_ats": ["greenhouse"]}},
        applicant="jane",
        dry_run=True,
        mode="review",
    )


def test_dispatcher_opens_session_before_adapter_apply(monkeypatch):
    """RED: apply_to_job must call get_transport() and pass the yielded page
    to adapter.apply. Before the fix, adapter.apply receives page=None.
    """
    _install_fake_adapter(monkeypatch, _AdapterThatRecordsPage)
    _AdapterThatRecordsPage._received_page = None
    _FakeTransport.reset()

    # Patch get_transport to return our fake.
    import src.apply.transport as transport_mod
    monkeypatch.setattr(transport_mod, "get_transport", lambda config, kind: _FakeTransport())

    from src.apply.dispatcher import apply_to_job
    ctx = _sample_ctx()
    result = apply_to_job(
        "https://boards.greenhouse.io/example/jobs/12345",
        ctx,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )

    # Adapter must have received a non-None page (from the transport).
    received = _AdapterThatRecordsPage._received_page
    assert received is not None, "adapter.apply was called with page=None — transport not wired"
    assert isinstance(received, _RealPage), f"expected _RealPage from transport, got {type(received)}"
    # Transport open() was called at least once with the job URL.
    assert _FakeTransport.call_count == 1
    assert result.status == "submitted"


def test_dispatcher_end_to_end_uses_local_transport(monkeypatch):
    """Integration RED: verify LocalTransport.session yields a page that
    reaches the adapter (through the seam-selected transport factory).
    """
    _install_fake_adapter(monkeypatch, _AdapterThatRecordsPage)
    _AdapterThatRecordsPage._received_page = None

    # Fake browser.session so LocalTransport's inner import + yield works.
    browser_mod = pytypes.ModuleType("browser")

    from contextlib import contextmanager as _cm

    @_cm
    def _fake_session(headless, storage_state_path):
        yield (_RealPage("local-transport-page"), None)

    browser_mod.session = _fake_session
    monkeypatch.setitem(sys.modules, "browser", browser_mod)

    from src.apply.dispatcher import apply_to_job

    ctx = _sample_ctx()
    result = apply_to_job(
        "https://boards.greenhouse.io/example/jobs/12345",
        ctx,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )

    received = _AdapterThatRecordsPage._received_page
    assert received is not None
    assert isinstance(received, _RealPage)
    assert received.marker == "local-transport-page"
    assert result.status == "submitted"
