"""
apply.transport — pluggable Playwright session provider (S10).

Two concrete implementations sit behind the `Transport` Protocol:

  - `LocalTransport`     — thin wrapper over S4's `browser.session()`.
  - `BrowserbaseTransport` — Browserbase cloud session with `solve_captchas`,
                             `proxies`, `block_ads` all ON (Q_BB2 + Q_BB3).

Dispatcher (S8 today, S17 wires it) calls `get_transport(config, kind)` where
`kind` is the CAPTCHA discriminator from S9 (`CaptchaKind | None`). Routing:

  kind is None                                                 → LocalTransport
  kind not None AND apply.captcha_transport == "browserbase"
    AND apply.browserbase.enabled                              → BrowserbaseTransport
  otherwise (kind not None, transport is local / bb disabled)  → LocalTransport
                                                                  (S13 fast-path
                                                                   escalation then
                                                                   handles the
                                                                   captcha-locked
                                                                   page in email)

The factory reads config on every call — no cached global — per L14: the same
`get_transport` that reads `apply.allowed_ats` must also read
`apply.captcha_transport` each dispatch, not from a module-load snapshot.

Landmine notes:
- L12: concrete classes are resolved via `importlib.import_module` on a string
  map, NOT held as class objects in the registry. This keeps test patching of
  the module-level `_client_factory` / `_playwright_factory` seams effective
  because the class is re-looked-up on each `get_transport()` call.
- L5: the release/close/stop teardown triple lives in the concrete transport,
  not here. See `browserbase.py` for the nested try/finally.
- L6: no `datetime.utcnow()` anywhere in this module tree.
- L7: log events emit only the whitelisted key set — no cookies, no URLs, no
  storage_state contents, no page bodies.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, ContextManager, Literal, Protocol

import structlog

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - purely for editors / type-checkers
    from playwright.sync_api import Page


class TransportConfigError(RuntimeError):
    """Raised when a transport cannot start because required config/env is missing.

    Always raised at `open()` call time, NEVER at import time — the test suite
    imports these modules without Browserbase credentials.
    """


@dataclass(frozen=True)
class TransportSession:
    """
    Yielded by every `Transport.open()` context manager.

    Field-by-field match with master-plan §4.3 `SessionContext`:
      transport, replay_url, proxies_enabled, solve_captchas.

    `page` carries the live Playwright `Page` handle — its type is stringly
    annotated below so importing this module never triggers a Playwright
    import at type-eval time (though Playwright is a hard dep anyway).
    """

    page: "Page"
    replay_url: str | None
    transport: Literal["local", "browserbase"]
    proxies_enabled: bool
    solve_captchas: bool


class Transport(Protocol):
    """Structural type for local + cloud transports.

    Implementations MUST return a context manager whose `__enter__` yields a
    `TransportSession`, and whose `__exit__` guarantees:
      - any provider-side session release ping (Browserbase REQUEST_RELEASE),
      - Playwright browser + playwright teardown,
      even on exception paths (L5 nested try/finally).
    """

    def open(
        self, url: str, storage_state: dict | None
    ) -> "ContextManager[TransportSession]": ...


# ── Factory ──────────────────────────────────────────────────────────────────
# L12: string map keyed on importable dotted-name → class-name. Resolved via
# `importlib.import_module` at call time. Class objects are never cached —
# patching `apply.transport.browserbase.BrowserbaseTransport` in a test still
# takes effect because the class is re-looked-up per call.

_TRANSPORT_REGISTRY: dict[str, tuple[str, str]] = {
    "local": ("src.apply.transport.local", "LocalTransport"),
    "browserbase": ("src.apply.transport.browserbase", "BrowserbaseTransport"),
}


def _resolve_transport(name: str) -> Transport:
    """L12-compliant resolver: importlib.import_module + getattr, no caching."""
    mod_path, cls_name = _TRANSPORT_REGISTRY[name]
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls()


def get_transport(config: dict, kind: str | None) -> Transport:
    """
    Route to the right Transport based on config + CAPTCHA discriminator.

    Args:
      config: The full loaded settings dict (must contain `apply` key when
              CAPTCHA routing is in play).
      kind:   The `CaptchaKind` from S9's `detect(page)`, or `None` when no
              CAPTCHA was detected.

    Returns:
      A fresh `Transport` instance. No caching (L14) — the same call site
      re-evaluates `apply.captcha_transport` and `apply.browserbase.enabled`
      on every dispatch so live config edits (e.g. someone toggles the
      Browserbase kill-switch) take effect on the next apply.

    Routing rules (locked in AC #8):
      - `kind is None`                                   → `LocalTransport`
      - `kind` + `apply.captcha_transport == "browserbase"`
        + `apply.browserbase.enabled is True`            → `BrowserbaseTransport`
      - otherwise (kind set but local mode / bb disabled) → `LocalTransport`
        The dispatcher (S8/S17) then decides whether to escalate via S13
        fast-path email — that's NOT this shard's concern.
    """
    if kind is None:
        return _resolve_transport("local")

    apply_cfg = config.get("apply") or {}
    if apply_cfg.get("captcha_transport") != "browserbase":
        return _resolve_transport("local")

    bb_cfg = apply_cfg.get("browserbase") or {}
    if not bb_cfg.get("enabled"):
        return _resolve_transport("local")

    # H12 fix: even when routing says browserbase, a resolver failure (env
    # missing, module not importable) must degrade to LocalTransport rather
    # than propagate — the pipeline is never-blocking. Emit an audit event
    # so operators can spot the degrade.
    try:
        return _resolve_transport("browserbase")
    except Exception as exc:  # noqa: BLE001 — resolver-side failure
        _log.warning(
            "apply.transport.browserbase_fallback",
            reason=type(exc).__name__,
        )
        return _resolve_transport("local")


# ── Re-exports (must run AFTER the factory infra is defined) ──────────────────
# These concrete imports run at package-load time. They MUST NOT touch
# `os.environ` for Browserbase credentials or construct any SDK client — AC #7
# is BLOCKING on that.
from .browserbase import BrowserbaseTransport  # noqa: E402
from .local import LocalTransport  # noqa: E402

__all__ = [
    "BrowserbaseTransport",
    "LocalTransport",
    "Transport",
    "TransportConfigError",
    "TransportSession",
    "get_transport",
]
