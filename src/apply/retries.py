"""
apply/retries.py — Shared retry decorator for auto-apply navigation + network calls.

Spec: .agent/one-big-feature/auto-apply-2026-07-06/03-specs/11-s11-retries-wrapper.md

Two decorators are exported:

  @navigation_retry
      Retries transient navigation / network exceptions (Playwright timeouts,
      httpx errors, ConnectionError, or any RetryableError). Three attempts
      with exponentially-jittered backoff (initial=1s, max=8s). On final
      failure the ORIGINAL exception is reraised — never `tenacity.RetryError`.
      Emits a `apply.retry` structlog warning per retry attempt with the keys:
          attempt, max_attempts, wait_seconds, exception_type, callable_name.
      No `args`, `kwargs`, `return_value`, or candidate profile fields ever
      touch the log record (L7 — no PII).

  @submit_no_retry
      No-op marker decorator whose docstring encodes the contract that a
      wrapped call MUST NEVER be retried. Adapter submit-form clicks (S8, S18)
      are decorated with this so a code reviewer can grep for the invariant.
      Retrying a submit risks double-application (variation-B finding,
      judge-output §Winner tiebreaker).

Callers decide how to map the reraised exception to the Contract §4.1 `Status`
literal (typically `"failed"`); this decorator is dispatcher-agnostic (L14).

Design constraints honored:
  L6  — the deprecated ``utcnow`` API is not used; structlog's timestamper owns time.
  L7  — no PII in the log event.
  L12 — ``functools.wraps`` preserved so importable names survive the wrap.
  L14 — no hardcoded ATS names or adapter dispatch tables in this module.
"""

from __future__ import annotations

import functools
from typing import Callable, ParamSpec, TypeVar

import httpx
import structlog
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

__all__ = [
    "RetryableError",
    "RETRYABLE_EXCEPTIONS",
    "navigation_retry",
    "submit_no_retry",
]

log = structlog.get_logger()


class RetryableError(Exception):
    """Base sentinel that any shard can raise to opt-in to `@navigation_retry`.

    Prefer raising a concrete transport exception (httpx.*, PlaywrightTimeoutError,
    ConnectionError) when the underlying error is transport-shaped; use this class
    only when a domain-level error is legitimately retryable and there's no
    transport-shaped exception to raise instead.
    """


# ── RETRYABLE_EXCEPTIONS: what @navigation_retry will retry on ──
#
# DEVIATION FROM SPEC §Acceptance 2.
# ---------------------------------
# The spec locked this to exactly
#   (PlaywrightTimeoutError, httpx.HTTPError, httpx.TimeoutException,
#    ConnectionError, RetryableError)
# but that set is incompatible with the retrofit acceptance criteria
# (§8 "behavior-preserving" and §14 "gmail suite passes zero regressions"):
#   * ``googleapiclient.errors.HttpError`` — Gmail's 500/503 shape;
#     retried by the pre-retrofit ``_retry_call``; NOT a subclass of any
#     spec-tuple member (runtime-verified). Excluding it means every
#     Gmail transient failure now hard-fails.
#   * ``google.auth.exceptions.TransportError`` — same story for
#     ``creds.refresh(Request())``.
#   * ``httpx.HTTPError`` sweeps in ``httpx.HTTPStatusError`` (4xx
#     client errors), which SHOULD NOT retry. Narrowed to
#     ``httpx.TransportError``, which covers ``ConnectError``,
#     ``TimeoutException``, ``NetworkError``, and ``RemoteProtocolError``.
#   * ``httpx.TimeoutException`` is a subclass of ``httpx.TransportError``
#     and is dropped as redundant.
#
# The reviewer resolves the internal spec conflict at merge time. Test
# ``test_retryable_exceptions_tuple_contract`` asserts REQUIRED members
# are present rather than pinning the exact tuple, so future shards can
# extend it.
#
# Import guard: ``apply/retries.py`` must stay importable in Playwright-
# only environments where the google client libs aren't installed. If a
# google lib is missing, its exception type is silently omitted from the
# tuple (retry-if-exception-type ignores absent types via isinstance).

_optional_retryable: list[type[BaseException]] = []
try:
    from googleapiclient.errors import HttpError as _GAPIHttpError
    _optional_retryable.append(_GAPIHttpError)
except ImportError:  # pragma: no cover — Playwright-only environment
    pass
try:
    from google.auth.exceptions import TransportError as _GAuthTransportError
    _optional_retryable.append(_GAuthTransportError)
except ImportError:  # pragma: no cover — Playwright-only environment
    pass

RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PlaywrightTimeoutError,
    httpx.TransportError,
    ConnectionError,
    RetryableError,
    *_optional_retryable,
)

# ── Retry config (constants — no knobs) ────────────────────────
_MAX_ATTEMPTS = 3
_BACKOFF_INITIAL_SECONDS = 1
_BACKOFF_MAX_SECONDS = 8

P = ParamSpec("P")
R = TypeVar("R")


def _qualified_type_name(exc: BaseException | None) -> str:
    """Fully-qualified module.qualname for an exception.

    Prevents observability filter collisions: playwright's
    ``TimeoutError`` has ``__name__ == "TimeoutError"`` — identical to
    the builtin ``TimeoutError`` and to ``asyncio.TimeoutError``.
    Emitting ``playwright._impl._errors.TimeoutError`` disambiguates
    (finding #7).
    """
    if exc is None:
        return "None"
    tp = type(exc)
    return f"{tp.__module__}.{tp.__qualname__}"


def _log_retry(retry_state) -> None:
    """Emit the `apply.retry` structlog event before each backoff sleep.

    Fires AFTER an attempt has failed and BEFORE the wait for the next attempt,
    so on a 3-attempt failure exactly two `apply.retry` events are emitted
    (before-sleep after attempts 1 and 2; the 3rd raises without a sleep).

    Only structural metadata is logged — never call arguments, return values,
    or profile fields (L7).
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    fn = getattr(retry_state, "fn", None)

    wait_seconds: float = 0.0
    if retry_state.next_action is not None:
        wait_seconds = float(getattr(retry_state.next_action, "sleep", 0.0) or 0.0)

    log.warning(
        "apply.retry",
        attempt=retry_state.attempt_number,
        max_attempts=_MAX_ATTEMPTS,
        wait_seconds=wait_seconds,
        exception_type=_qualified_type_name(exc),
        callable_name=fn.__name__ if fn is not None else "unknown",
    )


def _build_decorator(before_sleep_extra: Callable[[object], None] | None) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Build the actual retry decorator, optionally chaining an extra
    before-sleep hook after the log emitter.

    Both ``_log_retry`` and ``before_sleep_extra`` are called under
    try/except-and-log guards. If either raises, tenacity's ``before_sleep``
    would propagate the exception and abort the retry loop — the wrapped
    call would never see attempts 2/3 and the caller would see the
    hook's exception instead of the ORIGINAL transient error. Swallowing
    hook faults here preserves the retry contract.
    """
    if before_sleep_extra is None:
        def before_sleep(retry_state):
            try:
                _log_retry(retry_state)
            except Exception as exc:  # pragma: no cover — defensive
                # Never let logging break the retry contract.
                log.error("apply.retry.log_failed", error=str(exc))
    else:
        def before_sleep(retry_state):
            try:
                _log_retry(retry_state)
            except Exception as exc:  # pragma: no cover — defensive
                log.error("apply.retry.log_failed", error=str(exc))
            try:
                before_sleep_extra(retry_state)
            except Exception as exc:  # pragma: no cover — defensive
                # Never let a broken recovery hook mask the original
                # transient exception — retry continues; caller sees
                # the underlying transport error on final failure.
                log.error("apply.retry.before_sleep_extra_failed", error=str(exc))

    def _apply(func: Callable[P, R]) -> Callable[P, R]:
        return retry(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential_jitter(initial=_BACKOFF_INITIAL_SECONDS, max=_BACKOFF_MAX_SECONDS),
            retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
            reraise=True,
            before_sleep=before_sleep,
        )(func)

    return _apply


def navigation_retry(
    _func: Callable[P, R] | None = None,
    *,
    before_sleep_extra: Callable[[object], None] | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry a navigation / network call on transient failures.

    Three attempts total; the ORIGINAL exception reraises on final failure
    (never `tenacity.RetryError`). Non-`RETRYABLE_EXCEPTIONS` propagate on
    the first raise.

    Works on plain functions and on instance / class methods (`self` /
    `cls` are passed through untouched).

    Usage:

        @navigation_retry
        def foo(): ...

        @navigation_retry(before_sleep_extra=_refresh_client)
        def bar(self): ...

    ``before_sleep_extra`` is called with the tenacity ``RetryCallState``
    AFTER the `apply.retry` log event fires and BEFORE the backoff sleep.
    Its intended use is stateful recovery on a bound method — access
    ``retry_state.args[0]`` for ``self`` and call a resource-refresh
    method (finding #2 — mid-retry OAuth refresh for the Gmail client).
    """
    if _func is not None and before_sleep_extra is None:
        # Plain form: @navigation_retry
        return _build_decorator(None)(_func)
    # Factory form: @navigation_retry(before_sleep_extra=...)
    if _func is not None:
        raise TypeError(
            "navigation_retry: cannot pass a callable positionally when "
            "before_sleep_extra is set; use @navigation_retry(before_sleep_extra=...)"
        )
    return _build_decorator(before_sleep_extra)


def submit_no_retry(func: Callable[P, R]) -> Callable[P, R]:
    """MARKER: this call must NEVER be retried; propagate exceptions immediately.

    A no-op passthrough. Its presence at a call site is the human-readable
    contract for the review pass — retrying a submit-form click risks
    double-application (variation-B finding #7, judge-output §Winner
    tiebreaker).
    """

    @functools.wraps(func)
    def _passthrough(*args: P.args, **kwargs: P.kwargs) -> R:
        return func(*args, **kwargs)

    return _passthrough
