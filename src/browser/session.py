"""
browser/session.py — the single Playwright session context manager.

Every browser-touching caller in the codebase — the JD scraper today, and
every apply adapter under `src/apply/*` tomorrow — opens Chromium through
this one primitive. This eliminates the "Playwright torn down per fetch"
anti-pattern from Phase 1 and freezes the try/finally shape that closes
the Chromium leak on setup failure (landmine L5).

Contract, frozen per spec §Interfaces:

    with session(
        *,
        headless: bool = True,
        storage_state_path: Path | None = None,
        user_agent: str | None = None,
        viewport: dict | None = None,
        trace_dir: Path | None = None,
    ) as (page, trace_path_or_None):
        ...

Guarantees:
  * `browser.new_context()` is inside a top-level try/finally — if it
    raises, `browser.close()` and `pw.stop()` still run (L5).
  * `sync_playwright().start()` is called exactly once per session().
  * If `storage_state_path` exists on entry, session state is hydrated;
    if it doesn't, no error is raised — a fresh state is written on exit.
  * Any file written by this shard has mode 0o600; any directory it creates
    has mode 0o700 (§12.3, Ben Q6).
  * No log record ever contains a URL, a cookie value, a user_agent, or any
    other content derivable from `Page.content()` (L7). Only event names.
  * `datetime.now(timezone.utc)` — never the deprecated tz-naive utcnow (L6).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import structlog
from playwright.sync_api import Page, sync_playwright

logger = structlog.get_logger(__name__)

# Frozen defaults — kept co-located so drift is visible in one diff.
_DEFAULT_VIEWPORT: dict = {"width": 1920, "height": 1080}
_DEFAULT_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


@contextmanager
def session(
    *,
    headless: bool = True,
    storage_state_path: Path | None = None,
    user_agent: str | None = None,
    viewport: dict | None = None,
    trace_dir: Path | None = None,
) -> Iterator[tuple[Page, Path | None]]:
    """Open Chromium, yield (page, trace_path_or_None), and tear down cleanly.

    See module docstring for the full contract. All parameters are
    keyword-only so callers can extend safely without positional-arg drift.
    """
    pw = sync_playwright().start()
    browser = None
    context = None
    trace_path: Path | None = None

    try:
        browser = pw.chromium.launch(headless=headless)

        new_context_kwargs: dict = {
            "user_agent": user_agent or _DEFAULT_UA,
            "viewport": viewport or _DEFAULT_VIEWPORT,
        }
        if storage_state_path is not None:
            candidate = Path(storage_state_path)
            if candidate.exists():
                new_context_kwargs["storage_state"] = str(candidate)

        context = browser.new_context(**new_context_kwargs)

        if trace_dir is not None:
            trace_dir_path = Path(trace_dir)
            trace_dir_path.mkdir(parents=True, exist_ok=True)
            os.chmod(trace_dir_path, 0o700)
            trace_path = trace_dir_path / f"{uuid.uuid4()}.zip"
            context.tracing.start(screenshots=True, snapshots=True, sources=False)

        page = context.new_page()
        # L7: name the event only. No URL / UA / state contents.
        logger.info(
            "browser.session.opened",
            headless=headless,
            has_state=bool(storage_state_path),
            trace_enabled=trace_path is not None,
        )
        yield page, trace_path
    finally:
        # Trace flush and storage_state write are best-effort — they must
        # never mask the caller's original exception (AC #7).
        try:
            if context is not None and trace_path is not None:
                try:
                    context.tracing.stop(path=str(trace_path))
                    if trace_path.exists():
                        os.chmod(trace_path, 0o600)
                    logger.info("browser.trace.saved")
                except Exception as e:
                    logger.warning(
                        "browser.trace.save_failed",
                        error_type=type(e).__name__,
                    )
            if context is not None and storage_state_path is not None:
                try:
                    state_target = Path(storage_state_path)
                    state_target.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(state_target.parent, 0o700)
                    context.storage_state(path=str(state_target))
                    os.chmod(state_target, 0o600)
                except Exception as e:
                    logger.warning(
                        "browser.storage_state.save_failed",
                        error_type=type(e).__name__,
                    )
        finally:
            # Teardown order matters: context before browser before pw.stop().
            if context is not None:
                try:
                    context.close()
                except Exception as e:
                    logger.warning(
                        "browser.context.close_failed",
                        error_type=type(e).__name__,
                    )
            if browser is not None:
                try:
                    browser.close()
                except Exception as e:
                    logger.warning(
                        "browser.browser.close_failed",
                        error_type=type(e).__name__,
                    )
            try:
                pw.stop()
            except Exception as e:
                logger.warning(
                    "browser.playwright.stop_failed",
                    error_type=type(e).__name__,
                )
            logger.info("browser.session.closed")


__all__ = ["session"]
