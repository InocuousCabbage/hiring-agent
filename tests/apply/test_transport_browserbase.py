"""
tests/apply/test_transport_browserbase.py — RED tests for BrowserbaseTransport
and the get_transport factory (S10).

Every test maps to an acceptance criterion in
.agent/one-big-feature/auto-apply-2026-07-06/03-specs/10-s10-browserbase-transport.md.

Coverage summary:
- AC #4: sessions.create called with locked browser_settings + keep_alive=False.
- AC #4/13: replay_url populates TransportSession.replay_url.
- AC #5: sessions.update(status="REQUEST_RELEASE") on normal AND exception ctx-exit.
- AC #5: teardown order — sessions.update → browser.close → playwright.stop.
- AC #4: cookies from storage_state seeded via context.add_cookies.
- AC #7: import succeeds without env; TransportConfigError raised only at open() time.
- AC #8: get_transport routing matrix (three cases).
- AC #11: log events carry no PII / no cookie or state contents (L7).

Testing seams (per spec §Interfaces + AC #10):
- `_client_factory()` — patched to return a fake Browserbase client.
- `_playwright_factory()` — patched to return a fake Playwright instance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = Path(__file__).parent.parent / "fixtures" / "apply"


# ── Fake Browserbase SDK ──────────────────────────────────────────────────────


class _FakeSessions:
    """Records sessions.create + sessions.update calls."""

    def __init__(self, canned_response: dict, call_log: list):
        self._canned = canned_response
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self._call_log = call_log

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._call_log.append(("sessions.create", kwargs))
        return SimpleNamespace(
            id=self._canned["id"],
            connect_url=self._canned["connect_url"],
            replay_url=self._canned["replay_url"],
            status=self._canned.get("status", "RUNNING"),
        )

    def update(self, session_id, **kwargs):
        self.update_calls.append((session_id, kwargs))
        self._call_log.append(("sessions.update", session_id, kwargs))
        return SimpleNamespace(
            id=session_id, status=kwargs.get("status", "REQUEST_RELEASE")
        )


class _FakeBrowserbaseClient:
    def __init__(self, canned_response: dict, call_log: list):
        self.sessions = _FakeSessions(canned_response, call_log)


# ── Fake Playwright ───────────────────────────────────────────────────────────


class _FakePage:
    def __init__(self, call_log: list):
        self._call_log = call_log
        self.goto_calls: list[str] = []

    def goto(self, url):
        self.goto_calls.append(url)
        self._call_log.append(("page.goto", url))


class _FakeContext:
    def __init__(self, call_log: list):
        self._call_log = call_log
        self._page = _FakePage(call_log)
        self.pages = [self._page]
        self.add_cookies_calls: list[list] = []

    def add_cookies(self, cookies):
        self.add_cookies_calls.append(cookies)
        self._call_log.append(("context.add_cookies", cookies))

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, call_log: list):
        self._call_log = call_log
        self._context = _FakeContext(call_log)
        self.contexts = [self._context]
        self.closed = False

    def close(self):
        self.closed = True
        self._call_log.append(("browser.close",))


class _FakeChromium:
    def __init__(self, call_log: list):
        self._call_log = call_log
        self.browser = _FakeBrowser(call_log)

    def connect_over_cdp(self, cdp_url):
        self._call_log.append(("chromium.connect_over_cdp", cdp_url))
        return self.browser


class _FakePlaywright:
    def __init__(self, call_log: list):
        self._call_log = call_log
        self.chromium = _FakeChromium(call_log)
        self.stopped = False

    def stop(self):
        self.stopped = True
        self._call_log.append(("playwright.stop",))


# ── Fixture helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def canned_response():
    return json.loads((FIXTURES / "browserbase_session_response.json").read_text())


@pytest.fixture
def call_log():
    return []


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_test_key_do_not_use")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj_test")


@pytest.fixture
def patched_transport(monkeypatch, canned_response, call_log, fake_env):
    """Patches both seams: _client_factory + _playwright_factory."""
    import apply.transport.browserbase as bb_mod

    fake_client = _FakeBrowserbaseClient(canned_response, call_log)
    fake_pw = _FakePlaywright(call_log)

    monkeypatch.setattr(bb_mod, "_client_factory", lambda: fake_client)
    monkeypatch.setattr(bb_mod, "_playwright_factory", lambda: fake_pw)

    return SimpleNamespace(
        module=bb_mod,
        client=fake_client,
        playwright=fake_pw,
        call_log=call_log,
    )


# ── AC #4: sessions.create shape ──────────────────────────────────────────────


def test_browserbase_open_calls_sessions_create_with_locked_settings(patched_transport):
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None):
        pass

    assert len(patched_transport.client.sessions.create_calls) == 1
    kwargs = patched_transport.client.sessions.create_calls[0]

    # AC #4 + landmine "browser_settings not exactly" (BLOCKING)
    assert kwargs["browser_settings"] == {
        "solve_captchas": True,
        "proxies": True,
        "block_ads": True,
    }
    # AC #4 + landmine keep_alive=True (BLOCKING)
    assert kwargs["keep_alive"] is False
    # BLOCKING: sessions.create must receive project_id
    assert kwargs["project_id"] == "proj_test"


# ── AC #4: replay_url propagation ─────────────────────────────────────────────


def test_browserbase_populates_replay_url(patched_transport, canned_response):
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None) as ts:
        assert ts.replay_url == canned_response["replay_url"]
        assert ts.replay_url == "https://browserbase.com/replay/abc123def456"
        assert ts.transport == "browserbase"
        assert ts.proxies_enabled is True
        assert ts.solve_captchas is True


# ── AC #5: REQUEST_RELEASE on normal exit ─────────────────────────────────────


def test_browserbase_release_called_on_normal_exit(patched_transport):
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None):
        pass

    assert len(patched_transport.client.sessions.update_calls) == 1
    session_id, kwargs = patched_transport.client.sessions.update_calls[0]
    assert session_id == "sess_abc123def456"
    assert kwargs["status"] == "REQUEST_RELEASE"
    # BLOCKING: project_id must be passed on sessions.update too.
    assert kwargs["project_id"] == "proj_test"


# ── AC #5: REQUEST_RELEASE on exception exit ──────────────────────────────────


def test_browserbase_release_called_on_exception(patched_transport):
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with pytest.raises(RuntimeError, match="boom"):
        with transport.open("https://x.example.com", None):
            raise RuntimeError("boom")

    assert len(patched_transport.client.sessions.update_calls) == 1
    session_id, kwargs = patched_transport.client.sessions.update_calls[0]
    assert kwargs["status"] == "REQUEST_RELEASE"
    assert kwargs["project_id"] == "proj_test"
    # Ensure Playwright + browser also torn down (L5).
    assert patched_transport.playwright.stopped is True
    assert patched_transport.playwright.chromium.browser.closed is True


# ── AC #5: teardown ordering (release → close → stop) ─────────────────────────


def test_browserbase_teardown_order(patched_transport):
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None):
        pass

    # Extract event names in order.
    events = [entry[0] for entry in patched_transport.call_log]

    # Sanity: create precedes update (obvious) and CDP-connect precedes goto.
    assert events.index("sessions.create") < events.index("sessions.update")
    assert events.index("chromium.connect_over_cdp") < events.index("page.goto")

    # Teardown order: sessions.update → browser.close → playwright.stop
    rel = events.index("sessions.update")
    close = events.index("browser.close")
    stop = events.index("playwright.stop")
    assert rel < close < stop, (
        f"Teardown order violation: sessions.update at {rel}, "
        f"browser.close at {close}, playwright.stop at {stop} — "
        f"full event log: {events}"
    )


# ── AC #4: cookies seeded from storage_state ──────────────────────────────────


def test_browserbase_seeds_cookies_from_storage_state(patched_transport):
    from apply.transport import BrowserbaseTransport

    cookies = [
        {"name": "gh_session", "value": "abcd1234", "domain": ".greenhouse.io", "path": "/"},
        {"name": "csrf", "value": "efgh5678", "domain": ".greenhouse.io", "path": "/"},
    ]
    storage_state = {"cookies": cookies, "origins": []}

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", storage_state):
        pass

    ctx = patched_transport.playwright.chromium.browser.contexts[0]
    assert len(ctx.add_cookies_calls) == 1
    assert ctx.add_cookies_calls[0] == cookies


def test_browserbase_no_cookies_when_storage_state_none(patched_transport):
    """AC #4 boundary: storage_state=None must NOT call context.add_cookies."""
    from apply.transport import BrowserbaseTransport

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None):
        pass

    ctx = patched_transport.playwright.chromium.browser.contexts[0]
    assert ctx.add_cookies_calls == []


# ── AC #7: env-var check at open() time, not import time ──────────────────────


def test_browserbase_import_succeeds_without_env(monkeypatch):
    """AC #7 (BLOCKING): `import` must not touch env or SDK client."""
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)

    # Force a fresh import.
    for mod in [
        "apply.transport.browserbase",
        "apply.transport.local",
        "apply.transport",
    ]:
        sys.modules.pop(mod, None)

    import apply.transport.browserbase  # noqa: F401 — must succeed


def test_browserbase_missing_env_raises_at_open_not_import(monkeypatch):
    """AC #7: TransportConfigError raised inside open() when env missing."""
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)

    from apply.transport import BrowserbaseTransport, TransportConfigError

    transport = BrowserbaseTransport()
    with pytest.raises(TransportConfigError):
        cm = transport.open("https://x", None)
        cm.__enter__()


def test_browserbase_missing_only_project_id_raises(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_test_key")
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)

    from apply.transport import BrowserbaseTransport, TransportConfigError

    transport = BrowserbaseTransport()
    with pytest.raises(TransportConfigError):
        cm = transport.open("https://x", None)
        cm.__enter__()


# ── AC #8: get_transport factory routing ──────────────────────────────────────


def test_get_transport_returns_local_when_kind_is_none():
    from apply.transport import LocalTransport, get_transport

    cfg = {"apply": {"captcha_transport": "browserbase", "browserbase": {"enabled": True}}}
    t = get_transport(cfg, None)
    assert isinstance(t, LocalTransport)


def test_get_transport_returns_browserbase_when_captcha_and_config_allow():
    from apply.transport import BrowserbaseTransport, get_transport

    cfg = {"apply": {"captcha_transport": "browserbase", "browserbase": {"enabled": True}}}
    t = get_transport(cfg, "cloudflare_turnstile")
    assert isinstance(t, BrowserbaseTransport)


def test_get_transport_returns_local_when_transport_local():
    from apply.transport import LocalTransport, get_transport

    cfg = {"apply": {"captcha_transport": "local", "browserbase": {"enabled": True}}}
    t = get_transport(cfg, "cloudflare_turnstile")
    assert isinstance(t, LocalTransport)


def test_get_transport_returns_local_when_browserbase_disabled():
    from apply.transport import LocalTransport, get_transport

    cfg = {"apply": {"captcha_transport": "browserbase", "browserbase": {"enabled": False}}}
    t = get_transport(cfg, "recaptcha_v2")
    assert isinstance(t, LocalTransport)


def test_get_transport_reads_config_every_call(monkeypatch):
    """L14: no cached global — mutating config between calls must be honored."""
    from apply.transport import BrowserbaseTransport, LocalTransport, get_transport

    cfg = {"apply": {"captcha_transport": "browserbase", "browserbase": {"enabled": True}}}
    assert isinstance(get_transport(cfg, "hcaptcha"), BrowserbaseTransport)

    cfg["apply"]["browserbase"]["enabled"] = False
    assert isinstance(get_transport(cfg, "hcaptcha"), LocalTransport)

    cfg["apply"]["browserbase"]["enabled"] = True
    cfg["apply"]["captcha_transport"] = "local"
    assert isinstance(get_transport(cfg, "hcaptcha"), LocalTransport)


# ── AC #11: log-event PII hygiene (L7) ────────────────────────────────────────

_ALLOWED_OPENED_KEYS = {
    "transport",
    "session_id",
    "replay_url",
    "proxies",
    "solve_captchas",
    "event",
}

_ALLOWED_RELEASED_KEYS = {
    "transport",
    "session_id",
    "release_status",
    "event",
}

_FORBIDDEN_KEYS = {
    "cookies",
    "storage_state",
    "value",
    "answer",
    "email",
    "phone",
    "url",  # never log target URLs (L7)
    "connect_url",
}


class _StructlogCapture:
    """Captures each structlog event as an event_dict via a processor.

    Raises DropEvent after capture so the record never reaches an underlying
    logger (structlog's default PrintLogger would reject our kwargs).
    """

    def __init__(self):
        self.records: list[dict] = []
        self._prev = None

    def _capture(self, logger, method_name, event_dict):
        self.records.append(dict(event_dict))
        raise structlog.DropEvent

    def __enter__(self):
        self._prev = structlog.get_config()
        structlog.configure(processors=[self._capture])
        return self

    def __exit__(self, exc_type, exc, tb):
        structlog.configure(**self._prev)
        return False


def test_log_events_contain_no_pii_keys(patched_transport):
    """AC #11 + L7: opened/released events carry only allowed keys."""
    from apply.transport import BrowserbaseTransport

    cookies = [
        {"name": "gh_session", "value": "SECRETVAL", "domain": ".greenhouse.io", "path": "/"},
    ]
    storage_state = {"cookies": cookies}

    with _StructlogCapture() as cap:
        transport = BrowserbaseTransport()
        with transport.open("https://x.example.com", storage_state):
            pass

    opened = [r for r in cap.records if r.get("event") == "apply.transport.opened"]
    released = [r for r in cap.records if r.get("event") == "apply.transport.released"]
    assert opened, f"missing apply.transport.opened; got: {cap.records}"
    assert released, f"missing apply.transport.released; got: {cap.records}"

    for rec in opened:
        keys = set(rec.keys())
        assert keys <= _ALLOWED_OPENED_KEYS, (
            f"apply.transport.opened has forbidden keys: {keys - _ALLOWED_OPENED_KEYS}"
        )
        assert rec["transport"] == "browserbase"
        assert rec["proxies"] is True
        assert rec["solve_captchas"] is True

    for rec in released:
        keys = set(rec.keys())
        assert keys <= _ALLOWED_RELEASED_KEYS, (
            f"apply.transport.released has forbidden keys: {keys - _ALLOWED_RELEASED_KEYS}"
        )

    # Belt-and-suspenders: no record anywhere leaked the cookie value.
    flat = json.dumps(cap.records, default=str)
    assert "SECRETVAL" not in flat, "cookie value leaked into logs"
    for banned in _FORBIDDEN_KEYS:
        for rec in cap.records:
            if rec.get("event", "").startswith("apply.transport."):
                assert banned not in rec, f"forbidden key '{banned}' in {rec.get('event')}"


# ── Sanity: no live Browserbase HTTP calls made ───────────────────────────────


def test_no_live_browserbase_http(monkeypatch, patched_transport):
    """Belt-and-suspenders: patch httpx and requests to explode on network call,
    then run the full open/close flow — no exception means no live traffic."""
    from apply.transport import BrowserbaseTransport

    def _explode(*a, **kw):
        raise AssertionError("LIVE HTTP CALL — test infra leaked past the seam")

    try:
        import httpx

        monkeypatch.setattr(httpx, "post", _explode)
        monkeypatch.setattr(httpx, "get", _explode)
    except ImportError:  # pragma: no cover
        pass
    try:
        import requests

        monkeypatch.setattr(requests, "post", _explode)
        monkeypatch.setattr(requests, "get", _explode)
    except ImportError:  # pragma: no cover
        pass

    transport = BrowserbaseTransport()
    with transport.open("https://x.example.com", None):
        pass
