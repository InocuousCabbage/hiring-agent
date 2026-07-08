"""
apply.transport.browserbase — BrowserbaseTransport (S10).

Opens a Browserbase cloud session with the Ben-locked posture
(`solve_captchas=True`, `proxies=True`, `block_ads=True`, `keep_alive=False`),
connects Playwright to the returned CDP endpoint, seeds cookies from any
storage_state passed in, then yields a `TransportSession` carrying the
Browserbase `replay_url` for downstream `ApplyResult.human_review_url`
population (S17).

## Contract shape (frozen by spec §Interfaces + AC #4/5/7/8/10/11)

  session = client.sessions.create(
      project_id=<env>,
      browser_settings={"solve_captchas": True, "proxies": True, "block_ads": True},
      keep_alive=False,
  )
  browser = playwright.chromium.connect_over_cdp(session.connect_url)
  context = browser.contexts[0]
  # seed cookies from storage_state.cookies
  page.goto(url)
  yield TransportSession(page, session.replay_url, "browserbase", True, True)
  # finally (nested try/finally triple per L5):
  client.sessions.update(session.id, project_id=<env>, status="REQUEST_RELEASE")
  browser.close()
  playwright.stop()

## Landmine posture (spec §Landmine-list)

- **L5** — the `create → connect → yield → release/close/stop` cycle sits
  inside a single top-level try/finally, and each teardown step is wrapped
  in its own try/finally so a failure in one teardown (e.g. release timeout)
  does not skip the next.
- **L6** — no `datetime.utcnow()` anywhere in this module. If we later stamp
  timestamps, use `datetime.now(timezone.utc)`.
- **L7** — logging never carries cookies, `storage_state` contents, URLs,
  page bodies, or `connect_url`. Only allowed keys emit:
    opened:   {transport, session_id, replay_url, proxies, solve_captchas}
    released: {transport, session_id, release_status}
- **L12** — no class-object caching. This module exports `BrowserbaseTransport`
  and `_client_factory` / `_playwright_factory` as module-level attributes so
  tests can monkeypatch them via `setattr`.
- **L14** — env vars are read INSIDE `open()` on every call. No module-level
  caching, no functools.cache, no globals() snapshot. Config-driven behavior
  stays honest across live edits.

## Test seams

`_client_factory()` — constructs a `Browserbase` SDK client. Tests replace
this with a fake that returns canned `.sessions.create` / `.sessions.update`
without touching the network.

`_playwright_factory()` — starts a Playwright instance. Tests replace this
with a fake `_FakePlaywright` implementing `.chromium.connect_over_cdp` and
`.stop()`.

Both seams are looked up as `module.attr` inside `open()` so `monkeypatch.setattr`
takes effect per-call.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import structlog

from . import TransportConfigError, TransportSession

logger = structlog.get_logger(__name__)


# ── Seams — patched by tests ──────────────────────────────────────────────────


def _client_factory():
    """Construct a Browserbase SDK client from env.

    Import happens inside the function so `import apply.transport.browserbase`
    at test-collection time never touches the Browserbase package (AC #7 —
    tests run with unset creds). Env is read here — the caller `open()` has
    already checked both variables are set.
    """
    from browserbase import Browserbase  # noqa: PLC0415 — lazy import (AC #7)

    return Browserbase(api_key=os.environ["BROWSERBASE_API_KEY"])


def _playwright_factory():
    """Start a Playwright instance for CDP connect.

    Lazy import for symmetry with `_client_factory`; Playwright is a hard dep
    so this always succeeds in a real environment.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    return sync_playwright().start()


# ── BrowserbaseTransport ──────────────────────────────────────────────────────


# Locked at spec-time per Ben Q_BB2 + Q_BB3 (variation-D §Approach). Any drift
# from this dict is a BLOCKING code-review finding.
_LOCKED_BROWSER_SETTINGS: dict = {
    "solve_captchas": True,
    "proxies": True,
    "block_ads": True,
}


class BrowserbaseTransport:
    """Browserbase-cloud transport, gated by CAPTCHA branch."""

    @contextmanager
    def open(self, url: str, storage_state: dict | None) -> Iterator[TransportSession]:
        # AC #7 + L14: check env at CALL time, not import time. Never cache.
        api_key = os.environ.get("BROWSERBASE_API_KEY")
        project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
        missing = [
            name
            for name, val in (
                ("BROWSERBASE_API_KEY", api_key),
                ("BROWSERBASE_PROJECT_ID", project_id),
            )
            if not val
        ]
        if missing:
            raise TransportConfigError(
                "Browserbase transport requires env vars: " + ", ".join(missing)
            )

        # These are looked up as `<module>.<attr>` so monkeypatch.setattr on
        # the module attribute takes effect. Do NOT `from . import
        # _client_factory` at module top — that would bind the callable to a
        # local name and defeat patching.
        from . import browserbase as _self_mod  # noqa: PLC0415 — re-lookup seam
        client = _self_mod._client_factory()
        pw = _self_mod._playwright_factory()

        session = None
        browser = None
        release_status: str | None = None

        # ── L5: outer try guards the entire browser-triple lifecycle. Nested
        # finallys inside guarantee release → browser.close → playwright.stop
        # runs regardless of where the exception happens.
        try:
            session = client.sessions.create(
                project_id=project_id,
                browser_settings=dict(_LOCKED_BROWSER_SETTINGS),
                keep_alive=False,
            )

            browser = pw.chromium.connect_over_cdp(session.connect_url)

            # SDK sample: Browserbase pre-creates a default context on connect.
            if getattr(browser, "contexts", None):
                context = browser.contexts[0]
            else:  # pragma: no cover — defensive: SDK contract may drift
                context = browser.new_context()

            # AC #4: seed cookies from storage_state, if present. L7: never log.
            if storage_state is not None:
                cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else None
                if cookies:
                    context.add_cookies(cookies)

            if getattr(context, "pages", None):
                page = context.pages[0]
            else:  # pragma: no cover
                page = context.new_page()

            # L7: no url, no state contents, no connect_url. Allowed keys only.
            logger.info(
                "apply.transport.opened",
                transport="browserbase",
                session_id=session.id,
                replay_url=session.replay_url,
                proxies=True,
                solve_captchas=True,
            )

            page.goto(url)

            yield TransportSession(
                page=page,
                replay_url=session.replay_url,
                transport="browserbase",
                proxies_enabled=True,
                solve_captchas=True,
            )

        finally:
            # ── L5 nested try/finally triple: release → browser.close → pw.stop
            # Each step protected so a failure in one doesn't skip the next.
            try:
                if session is not None:
                    try:
                        updated = client.sessions.update(
                            session.id,
                            project_id=project_id,
                            status="REQUEST_RELEASE",
                        )
                        release_status = getattr(updated, "status", "REQUEST_RELEASE")
                    except Exception as e:
                        # Do NOT re-raise — release is best-effort, but we still
                        # need to close the browser + stop Playwright below.
                        logger.warning(
                            "apply.transport.release_failed",
                            transport="browserbase",
                            error_type=type(e).__name__,
                        )
            finally:
                try:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception as e:
                            logger.warning(
                                "apply.transport.browser_close_failed",
                                transport="browserbase",
                                error_type=type(e).__name__,
                            )
                finally:
                    if pw is not None:
                        try:
                            pw.stop()
                        except Exception as e:
                            logger.warning(
                                "apply.transport.playwright_stop_failed",
                                transport="browserbase",
                                error_type=type(e).__name__,
                            )

            # AC #11: released event fires exactly once, allowed keys only.
            logger.info(
                "apply.transport.released",
                transport="browserbase",
                session_id=session.id if session is not None else None,
                release_status=release_status,
            )


__all__ = ["BrowserbaseTransport"]
