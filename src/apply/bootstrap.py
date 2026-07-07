"""
src/apply/bootstrap.py - Shard S7 bootstrap-cli.

Interactive CLI that turns a real, human-completed ATS login (including
MFA) into a re-usable encrypted `context.storage_state()` snapshot on
disk, keyed by ATS + user. Also ships `--status` which reports which
ATSes have a stored state and whether it is still fresh.

Entry points:
    python -m src.apply.bootstrap <ats>
    python -m src.apply.bootstrap --status
    python -m src.apply.bootstrap --user <name> <ats>

Exit codes:
    0    success (bootstrap saved) OR status printed
    2    unsupported ats
    3    timeout — operator did not finish login within 5 minutes
    4    headed browser could not launch (no display / missing deps)
    130  operator hit Ctrl-C

Discipline (paste from master-plan §10):
    L5: browser + context + page live inside a single try/finally so
        both context.close() and browser.close() run on every path
        (success, timeout, KeyboardInterrupt, unexpected exception).
    L6: never uses the deprecated naive-UTC helper; every timestamp
        is `datetime.now(timezone.utc).isoformat()`.
    L7: state / cookies / MFA codes are NEVER logged or printed. Only
        the ats + user + verified-at timestamp appear in operator
        output; the state dict itself is opaque to this module past
        `store_state`.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from src.apply.credentials import has_state, load_state, store_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen ATS registry (Phase 3: greenhouse only; Phase 3.5 adds lever/ashby)
# ---------------------------------------------------------------------------

# TODO(S8): validate marker against real Greenhouse post-login redirect
# during dry-run; adjust if brittle. `**/candidates/**` is a defensible
# best-guess that matches both the operator-facing candidates path AND
# any tenant-subdomain path segment.
_KNOWN_POST_LOGIN_MARKERS: dict[str, str] = {
    "greenhouse": "**/candidates/**",
}

# TODO(S8): confirm this is the canonical operator-login URL for
# Greenhouse candidate portal; if the demo board uses a tenant
# subdomain instead, extend to a per-tenant map.
_LOGIN_URLS: dict[str, str] = {
    "greenhouse": "https://boards.greenhouse.io/candidates/sign_in",
}

# Freshness threshold for `--status`: state older than this is flagged
# `(stale — re-bootstrap recommended)`. Spec §Acceptance #4.
_STALE_THRESHOLD = timedelta(days=30)

# Wall-clock timeout applied to `page.wait_for_url` — 5 minutes covers
# MFA challenges (SMS delivery, TOTP, hardware key) without hanging the
# browser indefinitely on operator inattention.
_DEFAULT_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Config loader (indirection point so tests can inject a fake)
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, Any]:
    """Load the apply-scoped config. Kept as a module-level indirection
    so tests can monkeypatch it — do NOT read a config file at module
    import time.
    """
    # Phase 3 fallback: only greenhouse is supported today. When S3's
    # config plumbing extends this module, this function will read
    # `config/settings.yaml -> apply.allowed_ats`.
    return {"apply": {"allowed_ats": ["greenhouse"]}}


# ---------------------------------------------------------------------------
# Wrap / unwrap — the storage envelope this module owns
# ---------------------------------------------------------------------------


def wrap_state(state: dict, user: str) -> dict:
    """Wrap Playwright's opaque `storage_state()` dict in a versioned
    envelope carrying the operator + verified-at timestamp.

    S6 stores whatever we give it. The wrap format is owned by S7 and
    consumed by S7 (`--status`) and S12 (review-loop).
    """
    return {
        "state": state,
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "user": user,
    }


def unwrap_state(wrapped: dict) -> tuple[dict, str, str]:
    """Reverse of `wrap_state`. Returns `(state, last_verified, user)`.

    Raises `ValueError` when the envelope is missing any required key
    — protects downstream consumers from silently reading garbage when
    an older version of this module wrote the state.
    """
    required = {"state", "last_verified", "user"}
    if not isinstance(wrapped, dict) or not required.issubset(wrapped.keys()):
        raise ValueError(
            f"storage envelope missing keys: expected {sorted(required)}, "
            f"got {sorted(wrapped.keys()) if isinstance(wrapped, dict) else type(wrapped).__name__}"
        )
    return wrapped["state"], wrapped["last_verified"], wrapped["user"]


# ---------------------------------------------------------------------------
# Bootstrap flow
# ---------------------------------------------------------------------------


def bootstrap_ats(
    ats: str,
    user: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Interactive bootstrap: open a headed browser at the ATS login
    page, let the operator complete login (including MFA), then snapshot
    `context.storage_state()` and persist it via S6.

    Returns exit code (0/2/3/4/130).
    """
    if ats not in _KNOWN_POST_LOGIN_MARKERS or ats not in _LOGIN_URLS:
        print(f"unsupported ats: {ats}", file=sys.stderr)
        return 2

    marker = _KNOWN_POST_LOGIN_MARKERS[ats]
    login_url = _LOGIN_URLS[ats]

    # sync_playwright() manages the driver process; the browser +
    # context + page live inside the inner try/finally (L5).
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            try:
                # headed only — MFA works because the operator sees the
                # browser. Any change to `headless=` here trips the
                # test_headless_never_true regex check.
                browser = playwright.chromium.launch(headless=False)
            except PlaywrightError as exc:
                logger.error("bootstrap.launch_failed", extra={"ats": ats})
                print(
                    "unable to launch headed Chromium — see SETUP.md "
                    f"'headed browser' prerequisites (details: {exc})",
                    file=sys.stderr,
                )
                return 4

            context = browser.new_context()
            page = context.new_page()
            # Wrap goto + wait_for_url together so a Ctrl-C during the
            # goto (which can block for seconds while the login page
            # loads) is still caught, printed, and mapped to exit 130.
            try:
                page.goto(login_url)
                logger.info(
                    "bootstrap.awaiting_login",
                    extra={
                        "ats": ats,
                        "user": user,
                        "timeout_seconds": timeout_seconds,
                    },
                )
                page.wait_for_url(marker, timeout=timeout_seconds * 1000)
            except PlaywrightTimeoutError:
                print(
                    f"bootstrap timed out after {timeout_seconds}s — "
                    "session not saved",
                    file=sys.stderr,
                )
                return 3
            except KeyboardInterrupt:
                print(
                    "bootstrap aborted by operator — session not saved",
                    file=sys.stderr,
                )
                return 130

            state = context.storage_state()
            wrapped = wrap_state(state, user)
            store_state(ats, user, wrapped)
            print(
                f"bootstrapped {ats} for {user} "
                f"(verified at {wrapped['last_verified']})"
            )
            return 0
        finally:
            # L5: both must close on every path (success, timeout,
            # KeyboardInterrupt, unexpected exception, launch fail).
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    logger.warning("bootstrap.context_close_failed", extra={"ats": ats})
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    logger.warning("bootstrap.browser_close_failed", extra={"ats": ats})


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------


def status(config: dict[str, Any], user: str | None = None) -> int:
    """Print one line per `apply.allowed_ats` entry describing whether
    a state is stored for the current user and, if so, whether it is
    still considered fresh (verified within the last 30 days)."""
    who = user or getpass.getuser()
    allowed = config.get("apply", {}).get("allowed_ats", [])
    now = datetime.now(timezone.utc)
    for ats in allowed:
        if not has_state(ats, who):
            print(f"{ats}: not bootstrapped")
            continue
        wrapped = load_state(ats, who)
        if wrapped is None:
            # has_state True but load_state None → treat as absent.
            print(f"{ats}: not bootstrapped")
            continue
        try:
            _state, last_verified, _stored_user = unwrap_state(wrapped)
        except ValueError:
            # Envelope is unreadable — surface as stale so the operator
            # re-bootstraps rather than silently trusting garbage.
            print(f"{ats}: bootstrapped, last_verified=unknown (stale — re-bootstrap recommended)")
            continue
        stale_suffix = ""
        try:
            parsed = datetime.fromisoformat(last_verified)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if now - parsed > _STALE_THRESHOLD:
                stale_suffix = " (stale — re-bootstrap recommended)"
        except (TypeError, ValueError):
            stale_suffix = " (stale — re-bootstrap recommended)"
        print(f"{ats}: bootstrapped, last_verified={last_verified}{stale_suffix}")
    return 0


# ---------------------------------------------------------------------------
# argparse entry
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.apply.bootstrap",
        description=(
            "Bootstrap ATS login state for auto-apply.\n\n"
            "Positional form:   python -m src.apply.bootstrap <ats>\n"
            "Status query form: python -m src.apply.bootstrap --status"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ats",
        nargs="?",
        default=None,
        help="ATS name to bootstrap (e.g. 'greenhouse')",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print one line per allowed_ats: bootstrapped-or-not + freshness",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Override the default user (getpass.getuser())",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    user = args.user or getpass.getuser()

    if args.status:
        return status(_load_config(), user=user)

    if not args.ats:
        parser.print_help(sys.stderr)
        return 2

    return bootstrap_ats(args.ats, user)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
