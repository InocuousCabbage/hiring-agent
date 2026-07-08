"""S2 shard — ATSAdapter Protocol + AdapterNotFoundError.

`ATSAdapter` is a runtime-checkable `typing.Protocol` (variation-A). It is
DELIBERATELY not an ABC — structural typing lets S8, S20, and future S22-S26
implement the Protocol without a common base-class inheritance edge that
would break test fakes.

Optional method `plan_form_fill(html, profile) -> list[FieldFill]` is
documented in the Protocol docstring but NOT declared on the Protocol itself
(variation-A two-layer split). Adapters that opt into the split provide it;
adapters that inline planning + driving into `apply()` do not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Playwright and ApplyContext are imported ONLY for type-checking to
    # avoid a runtime edge that would drag playwright into every
    # Protocol-consumer's import graph. Circular-safe: types.py never
    # imports base.py.
    from playwright.sync_api import Page

    from src.apply.types import ApplyContext, ApplyResult


class AdapterNotFoundError(LookupError):
    """Raised INTERNALLY by `dispatch()` when no registered/allowed adapter
    matches a URL. Caught by `apply_to_job()` which soft-fails into
    `ApplyResult(status='skipped', reason='no adapter for <domain>')`.
    Never propagates out of `apply_to_job`.
    """


class SessionExpiredError(RuntimeError):
    """Raised by an adapter / dispatcher when the keyring-stored
    ``storage_state`` for an ATS has gone stale (login required).

    The S17 seam catches this specifically so it can invoke
    ``notify_session_expired(ats, user, last_run_iso)`` (S13 fast-path
    email) BEFORE continuing to the next job. On merge day the S2
    dispatcher does not raise this itself — it soft-fails to
    ``ApplyResult(status='failed')`` — but the exception surface is
    reserved here so adapters or future dispatch refactors can
    distinguish session-expiry from generic navigation failure without
    parsing a `reason` string.

    Attributes:
        ats: the ATS slug (e.g. "greenhouse").
        last_run_iso: ISO8601 timestamp of the last successful login, or
            None if never.
    """

    def __init__(self, ats: str, last_run_iso: str | None = None):
        super().__init__(f"session expired for ats={ats}")
        self.ats = ats
        self.last_run_iso = last_run_iso


@runtime_checkable
class ATSAdapter(Protocol):
    """Structural protocol every ATS integration implements.

    Attributes:
        name: ATS slug (`"greenhouse"`, `"lever"`, `"ashby"`, `"generic"`,
            `"computer_use"`). Must be unique across `_ADAPTER_CLASSES`.
        domains: tuple of URL substrings the adapter claims. Dispatcher
            uses `detect()`, not `domains`, for the final match — `domains`
            is informational for logging and admin diagnostics.

    Methods:
        detect(url): return True iff this adapter should handle `url`.
        apply(page, ctx): navigate + fill + (submit or hand off to review).
            MUST return an `ApplyResult` — MUST NOT raise (any raise is
            soft-failed at the dispatcher layer to `status="failed"`).

        plan_form_fill(html, profile) -> list[FieldFill]: OPTIONAL
            variation-A pure-planner method. Not on the Protocol so
            adapters can opt out.
    """

    name: str
    domains: tuple[str, ...]

    def detect(self, url: str) -> bool: ...

    def apply(self, page: "Page", ctx: "ApplyContext") -> "ApplyResult": ...
