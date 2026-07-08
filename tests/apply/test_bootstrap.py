"""
tests/apply/test_bootstrap.py - Shard S7 bootstrap-cli RED tests.

Covers acceptance criteria 1-14 for src/apply/bootstrap.py per spec
`/Users/chiveschamoy/projects/hiring-agent/.agent/one-big-feature/
auto-apply-2026-07-06/03-specs/07-s7-bootstrap-cli.md`.

Isolation rule: NO test may launch a real browser or touch the real OS
keyring. `fake_playwright` fixture patches `sync_playwright` at
`src.apply.bootstrap.sync_playwright` with a hand-rolled fake that
yields controllable browser/context/page mocks. State-store calls are
patched at the module level (`src.apply.bootstrap.store_state /
load_state / has_state`) — never the real keyring.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path so `import src.apply.bootstrap` works
# under `pytest tests/apply/test_bootstrap.py -v` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePage:
    def __init__(self, wait_for_url_side_effect=None) -> None:
        self.goto_calls: list[str] = []
        self.wait_for_url_calls: list[tuple[str, int]] = []
        self._wait_for_url_side_effect = wait_for_url_side_effect

    def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    def wait_for_url(self, pattern: str, timeout: int = 0) -> None:
        self.wait_for_url_calls.append((pattern, timeout))
        if self._wait_for_url_side_effect is not None:
            raise self._wait_for_url_side_effect


class FakeContext:
    def __init__(self, storage_state_payload: dict | None = None) -> None:
        self._payload = storage_state_payload or {
            "cookies": [{"name": "x", "value": "y"}],
            "origins": [],
        }
        self.closed = False
        self.pages: list[FakePage] = []

    def new_page(self) -> FakePage:
        page = FakePage(wait_for_url_side_effect=self._wait_side_effect)
        self.pages.append(page)
        return page

    def storage_state(self) -> dict:
        return self._payload

    def close(self) -> None:
        self.closed = True

    # attribute installed by FakeBrowser to propagate side-effects
    _wait_side_effect = None


class FakeBrowser:
    def __init__(self, wait_side_effect=None, launch_side_effect=None) -> None:
        self.closed = False
        self.contexts: list[FakeContext] = []
        self._wait_side_effect = wait_side_effect
        # launch_side_effect handled by FakeChromium

    def new_context(self) -> FakeContext:
        ctx = FakeContext()
        ctx._wait_side_effect = self._wait_side_effect
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(
        self,
        wait_side_effect=None,
        launch_side_effect: Exception | None = None,
    ) -> None:
        self.launch_calls: list[dict] = []
        self._wait_side_effect = wait_side_effect
        self._launch_side_effect = launch_side_effect
        self.browser: FakeBrowser | None = None

    def launch(self, **kwargs) -> FakeBrowser:
        self.launch_calls.append(kwargs)
        if self._launch_side_effect is not None:
            raise self._launch_side_effect
        self.browser = FakeBrowser(wait_side_effect=self._wait_side_effect)
        return self.browser


class FakePlaywright:
    def __init__(
        self,
        wait_side_effect=None,
        launch_side_effect: Exception | None = None,
    ) -> None:
        self.chromium = FakeChromium(
            wait_side_effect=wait_side_effect,
            launch_side_effect=launch_side_effect,
        )
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePlaywrightCM:
    """Mimics `sync_playwright()` — a context manager yielding FakePlaywright."""

    def __init__(
        self,
        wait_side_effect=None,
        launch_side_effect: Exception | None = None,
    ) -> None:
        self.pw = FakePlaywright(
            wait_side_effect=wait_side_effect,
            launch_side_effect=launch_side_effect,
        )

    def __enter__(self) -> FakePlaywright:
        return self.pw

    def __exit__(self, *exc) -> None:
        self.pw.stop()


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wait_side_effect=None,
    launch_side_effect: Exception | None = None,
) -> FakePlaywrightCM:
    """Patch `src.apply.bootstrap.sync_playwright` to yield a FakePlaywright."""
    import src.apply.bootstrap as mod

    cm = FakePlaywrightCM(
        wait_side_effect=wait_side_effect,
        launch_side_effect=launch_side_effect,
    )
    monkeypatch.setattr(mod, "sync_playwright", lambda: cm)
    return cm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_state(monkeypatch: pytest.MonkeyPatch):
    """Neutralise credentials module — no test should reach real keyring."""
    import src.apply.bootstrap as mod

    monkeypatch.setattr(mod, "store_state", MagicMock(name="store_state"))
    monkeypatch.setattr(mod, "load_state", MagicMock(return_value=None))
    monkeypatch.setattr(mod, "has_state", MagicMock(return_value=False))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unsupported_ats_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from src.apply.bootstrap import main

    rc = main(["lever"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unsupported ats: lever" in err


def test_status_with_no_state_prints_not_bootstrapped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    monkeypatch.setattr(mod, "has_state", MagicMock(return_value=False))
    monkeypatch.setattr(mod, "load_state", MagicMock(return_value=None))
    # Use --config path via monkeypatched loader
    monkeypatch.setattr(
        mod,
        "_load_config",
        lambda: {"apply": {"allowed_ats": ["greenhouse"]}},
    )

    rc = main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "greenhouse: not bootstrapped" in out


def test_status_with_fresh_state_prints_verified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main, wrap_state

    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    wrapped = {
        "state": {"cookies": []},
        "last_verified": fresh,
        "user": "ben",
    }
    monkeypatch.setattr(mod, "has_state", MagicMock(return_value=True))
    monkeypatch.setattr(mod, "load_state", MagicMock(return_value=wrapped))
    monkeypatch.setattr(
        mod,
        "_load_config",
        lambda: {"apply": {"allowed_ats": ["greenhouse"]}},
    )

    rc = main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "greenhouse: bootstrapped, last_verified=" in out
    assert "(stale" not in out


def test_status_stale_marks_recommend_rebootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    wrapped = {"state": {}, "last_verified": stale, "user": "ben"}
    monkeypatch.setattr(mod, "has_state", MagicMock(return_value=True))
    monkeypatch.setattr(mod, "load_state", MagicMock(return_value=wrapped))
    monkeypatch.setattr(
        mod,
        "_load_config",
        lambda: {"apply": {"allowed_ats": ["greenhouse"]}},
    )

    rc = main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(stale — re-bootstrap recommended)" in out


def test_wrap_and_unwrap_state_roundtrip() -> None:
    from src.apply.bootstrap import unwrap_state, wrap_state

    state = {"cookies": [{"name": "x", "value": "y"}], "origins": []}
    wrapped = wrap_state(state, "ben")
    got_state, last_verified, user = unwrap_state(wrapped)
    assert got_state == state
    assert user == "ben"
    # Parses as ISO-8601 UTC
    parsed = datetime.fromisoformat(last_verified)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_unwrap_raises_on_schema_mismatch() -> None:
    from src.apply.bootstrap import unwrap_state

    with pytest.raises(ValueError):
        unwrap_state({"foo": "bar"})


def test_bootstrap_timeout_returns_3_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    _install_fake_playwright(
        monkeypatch, wait_side_effect=PlaywrightTimeoutError("timeout")
    )

    rc = main(["greenhouse"])
    err = capsys.readouterr().err
    assert rc == 3
    assert "bootstrap timed out after 300s" in err
    assert mod.store_state.call_count == 0


def test_bootstrap_success_calls_store_state_with_wrapped_dict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr("getpass.getuser", lambda: "ben")

    rc = main(["greenhouse"])
    assert rc == 0
    assert mod.store_state.call_count == 1
    args, _ = mod.store_state.call_args
    assert args[0] == "greenhouse"
    assert args[1] == "ben"
    wrapped = args[2]
    assert set(wrapped.keys()) == {"state", "last_verified", "user"}
    assert wrapped["user"] == "ben"
    assert isinstance(wrapped["state"], dict)
    # ISO-8601 UTC parse
    parsed = datetime.fromisoformat(wrapped["last_verified"])
    assert parsed.utcoffset() == timedelta(0)


def test_keyboard_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.apply.bootstrap import main

    cm = _install_fake_playwright(monkeypatch, wait_side_effect=KeyboardInterrupt())

    rc = main(["greenhouse"])
    out_err = capsys.readouterr()
    assert rc == 130
    assert "bootstrap aborted by operator" in out_err.err
    # Browser + context closed
    assert cm.pw.chromium.browser is not None
    assert cm.pw.chromium.browser.closed is True
    assert cm.pw.chromium.browser.contexts[0].closed is True


def test_browser_closed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from src.apply.bootstrap import main

    cm = _install_fake_playwright(
        monkeypatch, wait_side_effect=PlaywrightTimeoutError("timeout")
    )
    main(["greenhouse"])
    br = cm.pw.chromium.browser
    assert br is not None
    assert br.closed is True
    assert br.contexts[0].closed is True


def test_no_headed_display_exits_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    from src.apply.bootstrap import main

    _install_fake_playwright(
        monkeypatch,
        launch_side_effect=PlaywrightError("BrowserType.launch: Failed to launch"),
    )
    rc = main(["greenhouse"])
    err = capsys.readouterr().err
    assert rc == 4
    assert "SETUP.md" in err


def test_user_flag_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr("getpass.getuser", lambda: "default_user")
    rc = main(["--user", "alice", "greenhouse"])
    assert rc == 0
    args, _ = mod.store_state.call_args
    assert args[0] == "greenhouse"
    assert args[1] == "alice"


def test_default_user_is_getpass_getuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr("getpass.getuser", lambda: "bob")
    rc = main(["greenhouse"])
    assert rc == 0
    args, _ = mod.store_state.call_args
    assert args[1] == "bob"


def test_no_datetime_utcnow_used() -> None:
    """L6: datetime.utcnow() is deprecated in 3.12+."""
    src_path = _REPO_ROOT / "src" / "apply" / "bootstrap.py"
    text = src_path.read_text()
    assert "datetime.utcnow" not in text, (
        "L6 violation: bootstrap.py must not use datetime.utcnow(); "
        "use datetime.now(timezone.utc) instead."
    )


def test_no_state_in_log_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L7: never log the raw state dict / cookies / MFA codes."""
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    # Fake playwright with a sentinel-embedded storage_state payload.
    cm = _install_fake_playwright(monkeypatch)

    # Override the storage_state payload to embed our sentinel BEFORE
    # the bootstrap flow retrieves it. We do this by intercepting
    # `new_context()` on the launched browser.
    original_launch = cm.pw.chromium.launch

    sentinel = "LEAK_ME_SENTINEL_ZZZ"

    def leaky_launch(**kwargs) -> FakeBrowser:
        br = original_launch(**kwargs)
        orig_new_context = br.new_context

        def new_context(*a, **kw) -> FakeContext:
            ctx = orig_new_context(*a, **kw)
            ctx._payload = {
                "cookies": [{"name": "session", "value": sentinel}],
                "origins": [],
            }
            return ctx

        br.new_context = new_context  # type: ignore[method-assign]
        return br

    cm.pw.chromium.launch = leaky_launch  # type: ignore[method-assign]

    caplog.set_level("DEBUG")
    monkeypatch.setattr("getpass.getuser", lambda: "ben")
    rc = main(["greenhouse"])
    assert rc == 0
    assert sentinel not in caplog.text
    # Also assert we never printed it to stdout/stderr via a capsys shim
    # captured message list.
    for rec in caplog.records:
        assert sentinel not in rec.getMessage()


def test_keyboard_interrupt_during_goto_also_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: Ctrl-C during page.goto (before wait_for_url is
    entered — realistic window while the login page loads) still
    produces the friendly message + exit 130. Broadened catch scope
    per code-review pass."""
    import src.apply.bootstrap as mod
    from src.apply.bootstrap import main

    cm = _install_fake_playwright(monkeypatch)

    # Force FakePage.goto to raise KeyboardInterrupt when called.
    original_launch = cm.pw.chromium.launch

    def leaky_launch(**kwargs):
        br = original_launch(**kwargs)
        orig_new_context = br.new_context

        def new_context(*a, **kw):
            ctx = orig_new_context(*a, **kw)
            orig_new_page = ctx.new_page

            def new_page():
                page = orig_new_page()

                def boom_goto(url: str) -> None:
                    raise KeyboardInterrupt()

                page.goto = boom_goto  # type: ignore[method-assign]
                return page

            ctx.new_page = new_page  # type: ignore[method-assign]
            return ctx

        br.new_context = new_context  # type: ignore[method-assign]
        return br

    cm.pw.chromium.launch = leaky_launch  # type: ignore[method-assign]

    rc = main(["greenhouse"])
    err = capsys.readouterr().err
    assert rc == 130
    assert "bootstrap aborted by operator" in err
    assert mod.store_state.call_count == 0
    assert cm.pw.chromium.browser is not None
    assert cm.pw.chromium.browser.closed is True
    assert cm.pw.chromium.browser.contexts[0].closed is True


def test_headless_never_true() -> None:
    """Blocking: headless=True defeats MFA. Ensure the source contains
    headless=False exactly once and headless=True zero times."""
    src_path = _REPO_ROOT / "src" / "apply" / "bootstrap.py"
    text = src_path.read_text()
    assert "headless=True" not in text
    # Exactly one headless=False occurrence
    matches = re.findall(r"headless\s*=\s*False", text)
    assert len(matches) == 1, (
        f"Expected exactly one `headless=False`, found {len(matches)}"
    )


# ---------------------------------------------------------------------------
# Extra tests — traceability to spec §Acceptance criteria (bonus coverage)
# ---------------------------------------------------------------------------


def test_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """Acceptance #13: `--help` prints usage."""
    from src.apply.bootstrap import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    # argparse prints "usage:" prefix
    assert "usage" in out.lower()


def test_success_prints_bootstrapped_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance #1: success flow prints `bootstrapped greenhouse for <user>`."""
    from src.apply.bootstrap import main

    _install_fake_playwright(monkeypatch)
    monkeypatch.setattr("getpass.getuser", lambda: "ben")
    rc = main(["greenhouse"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bootstrapped greenhouse for ben" in out
    assert "verified at" in out


def test_known_post_login_markers_has_greenhouse() -> None:
    """Contract: `_KNOWN_POST_LOGIN_MARKERS` is populated for greenhouse
    (Phase 3), and the value is non-empty."""
    from src.apply.bootstrap import _KNOWN_POST_LOGIN_MARKERS, _LOGIN_URLS

    assert "greenhouse" in _KNOWN_POST_LOGIN_MARKERS
    assert _KNOWN_POST_LOGIN_MARKERS["greenhouse"]
    assert "greenhouse" in _LOGIN_URLS
    assert _LOGIN_URLS["greenhouse"].startswith("http")
