"""S2 shard — URL → ATSAdapter resolution + soft-fail `apply_to_job` entry.

Two rules this module MUST NOT break:

    L12: `_ADAPTER_CLASSES` is a STRING map (`ats_name -> "module_path:class_name"`).
         Resolution happens at CALL time via `importlib.import_module` +
         `getattr`, so test-monkeypatched module attributes are picked up
         without touching this file.

    L14: `apply.allowed_ats` is read from the config dict passed to EVERY call.
         There is NO module-level cache. Mutating the config between calls
         must be observed immediately.

Public surface:
    - `dispatch(url, config) -> ATSAdapter | None`
    - `apply_to_job(job_url, ctx, config) -> ApplyResult`   # never raises
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog

from src.apply.base import AdapterNotFoundError
from src.apply.types import ApplyResult

if TYPE_CHECKING:
    from src.apply.base import ATSAdapter
    from src.apply.types import ApplyContext


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Adapter registry (L12: STRING MAP — never class objects).
#
# Format: `ats_name -> "<module_path>:<class_name>"`. Resolved lazily at call
# time so test monkeypatching of the module attribute is picked up.
#
# Phase 3 MVP: greenhouse only.
# Phase 3.5:  add "lever", "ashby".
# Phase 3.6:  add "workday", "icims" (after Turnstile spike).
# Opt-in:     "computer_use" (S20, `apply.long_tail: computer_use`).
# ---------------------------------------------------------------------------

_ADAPTER_CLASSES: dict[str, str] = {
    "greenhouse": "src.apply.adapters.greenhouse:GreenhouseAdapter",
    # S20: opt-in long-tail fallback for unmatched ATS domains. Never selected
    # by URL detect() (empty domains tuple); only reachable via the
    # `apply.long_tail == "computer_use"` gate below.
    "computer_use": "src.apply.adapters.computer_use:ComputerUseAdapter",
}


# S20: allowed values for `apply.long_tail`. Validated on every dispatch call
# and re-validated by S3's `_validate_apply_config` on load.
_ALLOWED_LONG_TAIL: tuple[str, ...] = ("none", "computer_use")


class ConfigValidationError(ValueError):
    """Raised when `apply.long_tail` is not one of the allowed values.

    S3 owns the load-time validator (`src/main.py::_validate_apply_config`);
    S20 tightens the runtime dispatch-side allowlist so no dispatch call can
    silently accept an unknown fallback name.
    """


def validate_long_tail(value: str) -> str:
    """Return `value` if it is an allowed `apply.long_tail` string, else raise.

    Allowed: `("none", "computer_use")`. Case-sensitive. Any other string
    (including `""`, `"None"`, `"COMPUTER_USE"`) raises
    `ConfigValidationError`. See S20 spec §File-ownership.
    """
    if value not in _ALLOWED_LONG_TAIL:
        raise ConfigValidationError(
            f"apply.long_tail must be one of {list(_ALLOWED_LONG_TAIL)!r}, got {value!r}"
        )
    return value


def _long_tail(config: dict) -> str:
    """Read `config['apply']['long_tail']` on EVERY call — defaults to 'none'.

    Missing / malformed → 'none' (safe default; fallback disabled).
    """
    apply_cfg = config.get("apply") if isinstance(config, dict) else None
    if not isinstance(apply_cfg, dict):
        return "none"
    raw = apply_cfg.get("long_tail", "none")
    if not isinstance(raw, str):
        return "none"
    return raw


def _load_adapter(ats_name: str) -> "ATSAdapter":
    """Resolve `ats_name` → adapter instance via importlib at call time (L12).

    Never caches the class object. Every call re-resolves the module
    attribute so test monkeypatching sees fresh state.
    """
    spec = _ADAPTER_CLASSES.get(ats_name)
    if spec is None:
        raise AdapterNotFoundError(f"unknown ats: {ats_name}")
    module_path, _, class_name = spec.partition(":")
    if not module_path or not class_name:
        raise AdapterNotFoundError(f"malformed registry entry for {ats_name!r}: {spec!r}")
    module = importlib.import_module(module_path)
    adapter_cls = getattr(module, class_name)
    return adapter_cls()


def _allowed_ats(config: dict) -> list[str]:
    """Read `config['apply']['allowed_ats']` on EVERY call — L14.

    Missing / malformed → empty list (treated as "no ATS allowed"), never
    a hardcoded fallback.
    """
    apply_cfg = config.get("apply") if isinstance(config, dict) else None
    if not isinstance(apply_cfg, dict):
        return []
    allowed = apply_cfg.get("allowed_ats")
    if not isinstance(allowed, list):
        return []
    return [a for a in allowed if isinstance(a, str)]


def dispatch(url: str, config: dict) -> "ATSAdapter | None":
    """Return an adapter instance for `url`, or None if no allowed adapter matches.

    Reads `apply.allowed_ats` on every call (L14). An ATS name absent from
    `allowed_ats` is treated as unregistered even if importable.

    S20 long-tail fallback: if no per-ATS adapter matches AND
    `apply.long_tail == "computer_use"`, return `ComputerUseAdapter` as the
    last-resort catch-all. Default (`"none"`) leaves fallback disabled.

    Returns None (rather than raising) so `apply_to_job` can distinguish
    "no match" from "adapter blew up". Downstream callers that want the
    raise semantic should call `_load_adapter` directly (internal use only).
    """
    for ats_name in _allowed_ats(config):
        if ats_name not in _ADAPTER_CLASSES:
            # Config-listed but not registered — skip quietly; S3's config
            # validator is the right place to flag this on load.
            continue
        if ats_name == "computer_use":
            # H11 post-review: computer_use is a valid allowed_ats entry that
            # arms the long_tail fallback. It's never selected via the
            # per-ATS detect() iteration (empty domains tuple), only via the
            # long_tail gate below. Skip silently in the loop — the previous
            # `log.warning('apply.dispatch_allowed_ats_ignored_computer_use')`
            # fired on the intended H11 opt-in configuration, flooding logs.
            continue
        try:
            adapter = _load_adapter(ats_name)
        except (ImportError, AttributeError, AdapterNotFoundError):
            # Adapter module not present in this checkout (e.g. S8 not merged
            # yet, or a Phase-3.5 adapter listed pre-merge). Skip silently
            # — dispatcher never raises out of resolution.
            continue
        try:
            if adapter.detect(url):
                return adapter
        except Exception:  # noqa: BLE001 — detect() must never break dispatch
            # A broken detect() must not derail the loop; move on.
            continue

    # S20: no per-ATS match. Consider long-tail catch-all.
    # H11 fix: the fallback is still an ATS adapter and must respect the
    # apply.allowed_ats gate. If the operator hasn't opted 'computer_use'
    # into allowed_ats, we do NOT fire the fallback — even when
    # apply.long_tail == 'computer_use'.
    long_tail = validate_long_tail(_long_tail(config))
    if long_tail == "computer_use" and "computer_use" in _allowed_ats(config):
        try:
            return _load_adapter("computer_use")
        except (ImportError, AttributeError, AdapterNotFoundError):
            # Fallback module not present — treat as no-match rather than raising.
            return None
    return None


# ---------------------------------------------------------------------------
# SG1 (Phase 3 xhigh iter-1): per-run storage_state cache
#
# Motivation: `credentials.load_state()` touches the OS keyring / Secret
# Service on every call. A keyring hang here reintroduces the B4-class
# failure mode we just closed in the Group J OAuth fix — every apply in
# the pipeline would independently hit the same slow/hung endpoint.
#
# Solution: a module-level cache keyed on (ats, user). The cache lives for
# the process's lifetime; the seam layer resets it once per run_pipeline
# invocation to avoid staleness across long-lived processes.
# ---------------------------------------------------------------------------

_state_cache: dict[tuple[str, str], dict | None] = {}


def _cached_load_and_unwrap_state(ats: str, user: str) -> dict | None:
    """Return a cached storage_state for (ats, user), populating from
    `credentials.load_and_unwrap_state` on first call. `None` results are
    also cached so a keyring probe doesn't rerun on a subsequent job that
    happens to hit the same (ats, user) tuple.
    """
    key = (ats, user)
    if key in _state_cache:
        return _state_cache[key]
    # Import from src.apply.credentials at call time so tests that monkeypatch
    # `credentials.load_state` see the fresh binding (mirrors L12 discipline).
    from src.apply.credentials import load_and_unwrap_state

    result = load_and_unwrap_state(ats, user)
    _state_cache[key] = result
    return result


def _reset_state_cache() -> None:
    """Clear the per-run storage_state cache. Called by the seam layer at
    pipeline entry so long-lived processes never observe stale keyring data.
    Also used by tests to force a fresh probe.
    """
    _state_cache.clear()


def get_transport(config: dict, kind: str | None = None):
    """Module-level re-export of `src.apply.transport.get_transport`.

    Kept as a thin call-through (rather than a plain `from ... import
    get_transport` at module scope) so BOTH patch surfaces used by tests
    keep working:
      - `monkeypatch.setattr(dispatcher, "get_transport", ...)` replaces
        this name outright (M5's RED test patches the dispatcher module
        directly).
      - `monkeypatch.setattr(src.apply.transport, "get_transport", ...)`
        is picked up too, because the import below is resolved fresh on
        every call (existing H4 wiring tests patch the transport module).
    """
    from src.apply.transport import get_transport as _get_transport

    return _get_transport(config, kind)


def apply_to_job(job_url: str, ctx: "ApplyContext", config: dict) -> ApplyResult:
    """Public entry point called from `src/main.py:172` (S17 seam).

    Contract:
      - NEVER raises. Any exception from `dispatch` or `adapter.apply` is
        converted into an `ApplyResult` with `status="failed"` and
        `reason="<ExcType>: <msg>"` (§4 + Q_BB1 addendum).
      - No adapter match → `ApplyResult(status="skipped", reason="no adapter for <domain>")`.
      - Zero PII in the emitted log records (L7): only `ats`, `job_url` host,
        and structural event names.
    """
    try:
        adapter = dispatch(job_url, config)
    except Exception as exc:  # noqa: BLE001 — dispatch is meant to swallow, but defense-in-depth
        log.warning("apply.dispatch_failed", host=_host(job_url), reason=_exc_repr(exc))
        return ApplyResult(status="failed", reason=_exc_repr(exc))

    if adapter is None:
        reason = f"no adapter for {_host(job_url)}"
        log.info("apply.no_adapter", host=_host(job_url))
        return ApplyResult(status="skipped", reason=reason)

    # H4 fix: open a transport session and pass the real Page to adapter.apply.
    # get_transport() picks LocalTransport (default) or BrowserbaseTransport
    # based on config.apply.captcha_transport + apply.browserbase.enabled.
    # For the initial gate here we pass kind=None (no CAPTCHA detected yet);
    # if an adapter escalates a CAPTCHA it will surface via its own
    # captcha_escalated status.
    try:
        transport = get_transport(config, kind=None)
    except Exception as exc:  # noqa: BLE001 — H12 covers graceful fallback; still soft-fail here.
        log.warning(
            "apply.transport_resolve_failed",
            ats=getattr(adapter, "name", None),
            host=_host(job_url),
            reason=_exc_repr(exc),
        )
        return ApplyResult(
            status="failed",
            ats=getattr(adapter, "name", None),
            reason=f"transport resolve failed: {_exc_repr(exc)}",
        )

    # H9: propagate SessionExpiredError to the seam. Every other exception is
    # soft-failed here so apply_to_job's `never raises` contract holds.
    from src.apply.base import SessionExpiredError

    # M5 + SG1/SG2/SE2/SD1 (Phase 3 xhigh iter-1):
    # Load bootstrapped storage_state via the shared credentials helper
    # (SE2 dedups the envelope-unwrap logic between here and review.py).
    # SG1: cache the per-(ats, user) result across a single pipeline run so
    # we don't touch the OS keyring on every apply — a keyring hang would
    # reintroduce the B4-class failure the Group J OAuth fix was written
    # to close.
    # SG2: helper does strict shape validation — malformed dicts never
    # reach transport.open() (returned as None instead).
    # SD1: helper's log lines carry only structural fields (`ats`, `user`,
    # `exc_type`) — never the exception message (which could carry
    # decrypted payload bytes from Fernet InvalidToken).
    storage_state = None
    ats_name = getattr(adapter, "name", None)
    if ats_name:
        user = getattr(ctx, "applicant", None) or config.get("apply", {}).get(
            "user", "single"
        )
        try:
            storage_state = _cached_load_and_unwrap_state(ats_name, user)
        except Exception as exc:  # noqa: BLE001 — never abort apply on lookup failure
            # SD1: log ONLY the exception type name, never `_exc_repr(exc)`
            # (which includes str(exc) and can carry decrypted payload bytes).
            log.warning(
                "apply.storage_state_load_failed",
                ats=ats_name,
                exc_type=type(exc).__name__,
            )
            storage_state = None

    try:
        with transport.open(job_url, storage_state=storage_state) as session:
            page = getattr(session, "page", None)
            try:
                result = adapter.apply(page=page, ctx=ctx)  # type: ignore[arg-type]
            except SessionExpiredError:
                raise  # H9: re-raise so the seam catches it and fires notify.
            except Exception as exc:  # noqa: BLE001 — soft-fail all other adapter exceptions
                log.warning(
                    "apply.adapter_exception",
                    ats=getattr(adapter, "name", None),
                    host=_host(job_url),
                    reason=_exc_repr(exc),
                )
                return ApplyResult(
                    status="failed",
                    ats=getattr(adapter, "name", None),
                    reason=_exc_repr(exc),
                )
    except SessionExpiredError:
        # H9: never swallow this — the seam's handler owns notify_session_expired.
        raise
    except Exception as exc:  # noqa: BLE001 — transport open() failure
        log.warning(
            "apply.transport_exception",
            ats=getattr(adapter, "name", None),
            host=_host(job_url),
            reason=_exc_repr(exc),
        )
        return ApplyResult(
            status="failed",
            ats=getattr(adapter, "name", None),
            reason=_exc_repr(exc),
        )

    if not isinstance(result, ApplyResult):
        # Structural violation by an adapter — treat as a failed apply.
        log.warning(
            "apply.adapter_bad_return",
            ats=getattr(adapter, "name", None),
            host=_host(job_url),
        )
        return ApplyResult(
            status="failed",
            ats=getattr(adapter, "name", None),
            reason=f"adapter returned non-ApplyResult: {type(result).__name__}",
        )
    return result


# ---------------------------------------------------------------------------
# Helpers — PII-safe log fodder only (L7)
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    """Return the URL hostname or `<no-host>` on parse failure. Never raises."""
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return "<no-host>"
    return parsed.hostname or "<no-host>"


def _exc_repr(exc: BaseException) -> str:
    """Format `<ExcType>: <msg>` — matches the test contract exactly."""
    return f"{type(exc).__name__}: {exc}"
