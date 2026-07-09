"""
apply.transport.local — LocalTransport (S10).

Thin wrapper over S4's `browser.session()` context manager. Yields a
`TransportSession` with `transport="local"`, `replay_url=None`, and both
`proxies_enabled` and `solve_captchas` False.

Storage-state handling: the Transport Protocol accepts a Playwright
`storage_state` dict (cookies + origins), and S4's `session()` reads state
from a FILE path. Post-I2-B3 (Phase 3 xhigh iter-2): this shard now
materializes the dict into a temp file with 0o600 mode, hands the path
to `browser.session(storage_state_path=...)`, and cleans up on exit.
Pre-fix: the dict was accepted for Protocol conformance and silently
dropped, so every local-mode apply opened an anonymous browser even
when the M5/SG1 dispatcher plumbing loaded bootstrapped credentials.

L7 discipline: NEVER logs cookie names, cookie values, or file
contents. Only the boolean `storage_state_present` is emitted.

The `import browser` is lazy so the S4 module doesn't need to exist at
S10-test-import time; tests inject a fake `browser` module into
`sys.modules` before calling `open()`.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
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

        # I2-B3: materialize the storage_state dict → temp file with 0o600.
        # `browser.session()` reads state from a file path (Playwright's
        # native form). Pre-fix: hardcoded None here, so the dict was
        # silently dropped and every apply ran anonymous.
        storage_state_path: str | None = None
        tmp_path: Path | None = None
        if isinstance(storage_state, dict) and storage_state:
            # Iter-3 F1 (Phase 3 xhigh iter-3): don't null out tmp_path on
            # failure — mkstemp already created the file; failure of the
            # subsequent write/chmod would otherwise ORPHAN the file with
            # partial or full state contents on disk. The outer finally
            # uses tmp_path to unlink; leaving it bound ensures cleanup
            # fires on every code path.
            try:
                fd, name = tempfile.mkstemp(suffix=".storage_state.json")
                os.close(fd)
                tmp_path = Path(name)
                tmp_path.write_text(json.dumps(storage_state))
                os.chmod(tmp_path, 0o600)
                storage_state_path = str(tmp_path)
            except Exception as exc:  # noqa: BLE001 — never block session on materialization
                # Log the failure structurally (SD1) and fall through to an
                # anonymous browser — same as pre-fix behavior, but at least
                # the operator sees a signal.
                logger.warning(
                    "apply.transport.storage_state_materialize_failed",
                    transport="local",
                    exc_type=type(exc).__name__,
                )
                storage_state_path = None
                # tmp_path INTENTIONALLY LEFT BOUND: the outer finally's
                # unlink will clean up any file mkstemp created (including
                # a partial-write orphan). Setting it to None here would
                # skip cleanup and leak the tmp file to disk.

        try:
            cm = browser.session(headless=True, storage_state_path=storage_state_path)

            with cm as opened:
                # S4 yields (page, trace_path_or_None).
                page, _trace_path = opened

                # L7: name-only, no URL, no state contents. I2-B3: also
                # surface a boolean `storage_state_present` for operator
                # visibility that credentials wired through.
                logger.info(
                    "apply.transport.opened",
                    transport="local",
                    session_id=None,
                    replay_url=None,
                    proxies=False,
                    solve_captchas=False,
                    storage_state_present=storage_state_path is not None,
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
                    # L7: name-only. Symmetric with `apply.transport.released`
                    # on BrowserbaseTransport so S16 log-invariant checks
                    # stay simple.
                    logger.info(
                        "apply.transport.released",
                        transport="local",
                        session_id=None,
                        release_status=None,
                    )
        finally:
            # I2-B3: cleanup materialized storage_state tempfile — never leave
            # decrypted cookies on disk past the session.
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


__all__ = ["LocalTransport"]
