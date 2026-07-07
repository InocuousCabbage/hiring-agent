"""
apply.transport.local — LocalTransport (S10).

Thin wrapper over S4's `browser.session()` context manager. Yields a
`TransportSession` with `transport="local"`, `replay_url=None`, and both
`proxies_enabled` and `solve_captchas` False.

Storage-state handling: the Transport Protocol accepts a Playwright
`storage_state` dict (cookies + origins), but S4's `session()` reads state
from a file path, not a dict. S17's seam-wiring shard owns the dict →
temp-file materialization for LocalTransport; today the dict is accepted
for Protocol conformance and dropped. Nothing in this shard writes cookie
values anywhere — including logs (L7).

The `import browser` is lazy so the S4 module doesn't need to exist at
S10-test-import time; tests inject a fake `browser` module into
`sys.modules` before calling `open()`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import structlog

from . import TransportSession

logger = structlog.get_logger(__name__)


class LocalTransport:
    """Playwright-local transport, backed by S4's `browser.session()`."""

    @contextmanager
    def open(self, url: str, storage_state: dict | None) -> Iterator[TransportSession]:
        # Lazy resolve — tests monkeypatch `sys.modules["browser"]`.
        import browser  # noqa: PLC0415 — deliberate lazy import (L12-friendly).

        # AC #3: session(storage_state_path=..., headless=True).
        # storage_state dict is Protocol-conformance sugar only; S17 owns the
        # dict → file materialization for local mode. See module docstring.
        cm = browser.session(headless=True, storage_state_path=None)

        with cm as opened:
            # S4 yields (page, trace_path_or_None).
            page, _trace_path = opened

            # L7: name-only, no URL, no state contents.
            logger.info(
                "apply.transport.opened",
                transport="local",
                session_id=None,
                replay_url=None,
                proxies=False,
                solve_captchas=False,
            )

            try:
                page.goto(url)
                yield TransportSession(
                    page=page,
                    replay_url=None,
                    transport="local",
                    proxies_enabled=False,
                    solve_captchas=False,
                )
            finally:
                # L7: name-only. Symmetric with `apply.transport.released` on
                # BrowserbaseTransport so S16 log-invariant checks stay simple.
                logger.info(
                    "apply.transport.released",
                    transport="local",
                    session_id=None,
                    release_status=None,
                )


__all__ = ["LocalTransport"]
