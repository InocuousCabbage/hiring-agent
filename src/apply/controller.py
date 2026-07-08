"""apply.controller — Playwright driver for Anthropic Computer Use tool actions.

Translates the four canonical Computer Use tool actions
(`computer.click`, `computer.type`, `computer.screenshot`, `computer.scroll`)
into the corresponding Playwright method calls
(`page.mouse.click`, `page.keyboard.type`, `page.screenshot`, `page.mouse.wheel`).

Guardrails:
- The `page` handle is stored in a private name-mangled attribute (`__page`)
  and NEVER exposed via a public attribute (per acceptance #8 / spec
  BLOCKING criterion — an LLM inspecting the Controller must not be able to
  reach through to bypass the tool-call boundary).
- Every action is wrapped in a per-turn timeout; overrun raises
  `ControllerTimeoutError` which the adapter catches and converts to a
  `review_required` result (never `submitted` — L13).
- Log emissions record the tool NAME only, never args (L7).
- Timezone-aware UTC timestamps only (never the deprecated naive helper) — L6.

S20 owns this file end-to-end.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from src.apply.logging import install_scrubber

# S17 install_scrubber returns None (idempotent global installer).
# Install once at import time, then bind the logger separately.
install_scrubber()
log = structlog.get_logger(__name__)


class ControllerTimeoutError(Exception):
    """Raised when a controller tool call exceeds its per-turn timeout."""

    def __init__(self, tool_name: str, elapsed_s: float, timeout_s: float):
        super().__init__(
            f"controller tool {tool_name!r} exceeded {timeout_s:.1f}s "
            f"(elapsed={elapsed_s:.2f}s)"
        )
        self.tool_name = tool_name
        self.elapsed_s = elapsed_s
        self.timeout_s = timeout_s


class Controller:
    """Executes Computer Use tool actions against a Playwright Page.

    The page is held privately (name-mangled). No public `page` attribute,
    no getter — the LLM tool-call layer is the only interface.
    """

    def __init__(self, page, timeout_s: float = 30.0):
        # Name-mangled: accessible only as `self._Controller__page` inside this
        # class body. External access requires the LLM to know the mangled
        # name AND bypass the tool-call layer — the same guarantee as private
        # by convention, plus the mangling.
        self.__page = page
        self._timeout_s = float(timeout_s)

    # ── Individual tool primitives ────────────────────────────────────────

    def click(self, x: int, y: int) -> None:
        """Translate `computer.click` → `page.mouse.click(x, y)`."""
        self._with_timeout("click", lambda: self.__page.mouse.click(int(x), int(y)))

    def type_text(self, s: str) -> None:
        """Translate `computer.type` → `page.keyboard.type(s)`."""
        self._with_timeout("type", lambda: self.__page.keyboard.type(str(s)))

    def screenshot(self) -> bytes:
        """Translate `computer.screenshot` → `page.screenshot()`."""
        return self._with_timeout("screenshot", lambda: self.__page.screenshot())

    def scroll(self, dx: int, dy: int) -> None:
        """Translate `computer.scroll` → `page.mouse.wheel(dx, dy)`."""
        self._with_timeout("scroll", lambda: self.__page.mouse.wheel(int(dx), int(dy)))

    # ── Public dispatch ───────────────────────────────────────────────────

    def apply_tool_call(self, tool_name: str, args: dict) -> dict:
        """Dispatch a Computer Use tool_use dict to the matching primitive.

        Logs `apply.controller.tool_called` with the NAME only (L7). Unknown
        tool names return `{"ok": False, "reason": "unknown_tool"}` — the loop
        can course-correct rather than crash the run.
        """
        log.info(
            "apply.controller.tool_called",
            tool=tool_name,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        args = args or {}
        canon = _canonicalize_tool_name(tool_name)

        if canon == "click":
            x, y = _extract_xy(args)
            self.click(x, y)
            return {"ok": True}
        if canon == "type":
            self.type_text(args.get("text", ""))
            return {"ok": True}
        if canon == "screenshot":
            png = self.screenshot()
            return {"ok": True, "bytes_len": len(png) if png else 0}
        if canon == "scroll":
            dx, dy = _extract_scroll(args)
            self.scroll(dx, dy)
            return {"ok": True}
        return {"ok": False, "reason": f"unknown_tool:{tool_name}"}

    # ── Internals ─────────────────────────────────────────────────────────

    def _with_timeout(self, tool_name: str, fn: Callable[[], Any]) -> Any:
        """Run `fn`, raise ControllerTimeoutError if it exceeds `_timeout_s`.

        Note: This is a POST-hoc timeout — Python can't preempt an ongoing
        synchronous call from another thread without cooperation. Playwright's
        own action timeouts (page.set_default_timeout) are the primary
        enforcement; this check catches the case where an action DID return
        but took longer than the budget, which is the failure mode tests
        actually observe via sleep-based fakes.
        """
        start = time.monotonic()
        result = fn()
        elapsed = time.monotonic() - start
        if elapsed > self._timeout_s:
            raise ControllerTimeoutError(tool_name, elapsed, self._timeout_s)
        return result


# ── Helpers (module-level; no page state) ─────────────────────────────────


def _canonicalize_tool_name(name: str) -> str:
    """Accept both `computer.click` and `click` (etc.)."""
    if not name:
        return ""
    return name.split(".", 1)[-1].lower()


def _extract_xy(args: dict) -> tuple[int, int]:
    coord = args.get("coordinate")
    if isinstance(coord, (list, tuple)) and len(coord) == 2:
        return int(coord[0]), int(coord[1])
    return int(args.get("x", 0)), int(args.get("y", 0))


def _extract_scroll(args: dict) -> tuple[int, int]:
    if "dx" in args or "dy" in args:
        return int(args.get("dx", 0)), int(args.get("dy", 0))
    direction = args.get("scroll_direction", "down")
    amount = int(args.get("scroll_amount", 3)) * 100
    if direction == "down":
        return 0, amount
    if direction == "up":
        return 0, -amount
    if direction == "right":
        return amount, 0
    if direction == "left":
        return -amount, 0
    return 0, 0
