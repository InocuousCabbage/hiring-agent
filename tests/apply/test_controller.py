"""tests/apply/test_controller.py — S20 Controller invariants.

Verifies tool-action translation, per-turn timeout, page-leak guard, and
landmine L6 / L7 for the controller module."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.apply.controller import (  # noqa: E402
    Controller,
    ControllerTimeoutError,
)


CONTROLLER_SRC = ROOT / "src" / "apply" / "controller.py"


class _FakePage:
    def __init__(self):
        self.mouse = MagicMock()
        self.keyboard = MagicMock()
        self._screenshot_bytes = b"\x89PNGfake"
        self.screenshot_called_with = None

    def screenshot(self, *args, **kwargs):
        self.screenshot_called_with = (args, kwargs)
        return self._screenshot_bytes


# ─── Tool-action → Playwright method translations ────────────────────────


def test_controller_translates_click_to_page_mouse_click():
    page = _FakePage()
    ctrl = Controller(page)
    ctrl.click(100, 200)
    page.mouse.click.assert_called_once_with(100, 200)


def test_controller_translates_type_to_keyboard_type():
    page = _FakePage()
    ctrl = Controller(page)
    ctrl.type_text("hello world")
    page.keyboard.type.assert_called_once_with("hello world")


def test_controller_translates_screenshot_to_page_screenshot():
    page = _FakePage()
    ctrl = Controller(page)
    result = ctrl.screenshot()
    assert result == b"\x89PNGfake"
    assert page.screenshot_called_with is not None


def test_controller_translates_scroll_to_mouse_wheel():
    page = _FakePage()
    ctrl = Controller(page)
    ctrl.scroll(10, -20)
    page.mouse.wheel.assert_called_once_with(10, -20)


def test_controller_apply_tool_call_dispatches_click():
    page = _FakePage()
    ctrl = Controller(page)
    out = ctrl.apply_tool_call("click", {"coordinate": [50, 75]})
    assert out["ok"] is True
    page.mouse.click.assert_called_once_with(50, 75)


def test_controller_apply_tool_call_accepts_dotted_names():
    page = _FakePage()
    ctrl = Controller(page)
    ctrl.apply_tool_call("computer.click", {"coordinate": (1, 2)})
    ctrl.apply_tool_call("computer.type", {"text": "abc"})
    ctrl.apply_tool_call("computer.screenshot", {})
    ctrl.apply_tool_call("computer.scroll", {"dx": 3, "dy": 4})
    page.mouse.click.assert_called_with(1, 2)
    page.keyboard.type.assert_called_with("abc")
    page.mouse.wheel.assert_called_with(3, 4)


def test_controller_apply_tool_call_unknown_returns_ok_false():
    page = _FakePage()
    ctrl = Controller(page)
    result = ctrl.apply_tool_call("wave", {})
    assert result["ok"] is False


# ─── Per-turn timeout ────────────────────────────────────────────────────


def test_controller_timeout_raises_controller_timeout_error(monkeypatch):
    page = _FakePage()

    def slow_click(x, y):
        import time
        time.sleep(0.15)  # > timeout_s below

    page.mouse.click = slow_click
    ctrl = Controller(page, timeout_s=0.05)
    with pytest.raises(ControllerTimeoutError) as excinfo:
        ctrl.click(1, 1)
    assert excinfo.value.tool_name == "click"
    assert excinfo.value.elapsed_s > 0.05


# ─── Page-handle leak guard ──────────────────────────────────────────────


def test_controller_never_leaks_page_outside_ctx():
    """No public `page` attribute; the raw handle is not reachable via
    `ctrl.page` / `getattr(ctrl, "page")` / `vars(ctrl)["page"]`."""
    page = _FakePage()
    ctrl = Controller(page)
    assert not hasattr(ctrl, "page"), "Controller.page is publicly exposed"
    # `page` NOT in ctrl.__dict__ keys (name-mangling hides it).
    assert "page" not in vars(ctrl)


def test_controller_never_leaks_page_in_source():
    """Belt-and-braces: source contains no `self.page =` assignment."""
    src = CONTROLLER_SRC.read_text()
    assert "self.page =" not in src


# ─── L6: no utcnow ───────────────────────────────────────────────────────


def test_controller_no_utcnow_L6():
    assert "datetime.utcnow" not in CONTROLLER_SRC.read_text()


# ─── L7: PII regression ──────────────────────────────────────────────────


def test_controller_pii_regression_L7(caplog):
    """Even when a tool-call includes a PII-shaped arg, the controller logs
    the tool NAME only. Arg values never appear in emitted log records."""
    page = _FakePage()
    ctrl = Controller(page)
    caplog.set_level(logging.INFO)
    ctrl.apply_tool_call("type", {"text": "candidate.email@example.test"})
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "candidate.email@example.test" not in joined
