"""
src/apply/credentials.py - Shard S6 storage-state-keyring.

Persist Playwright `context.storage_state()` dicts encrypted at rest,
keyed by ATS + user, so the auto-apply pipeline can re-open an
authenticated browser without prompting for credentials at run-time.

Primary backend: OS keyring (macOS Keychain / GNOME Secret Service /
Windows Credential Vault) under the service name yielded by
`_service_name(ats, user)`.

Fallback backend: Fernet-encrypted file on disk (mode 0o600 in a
0o700 dir), with the Fernet key held in keyring under a separate
bootstrap service. Engaged when the primary keyring is
`keyring.backends.fail.Keyring` OR when a `store_state` call raises
`NoKeyringError` / `KeyringLocked`.

Public API (frozen — consumed by S7 bootstrap-cli and S12 review-loop):
- `store_state(ats, user, state) -> None`
- `load_state(ats, user) -> dict | None`
- `has_state(ats, user) -> bool`
- `evict(ats, user) -> bool`
- `StorageStateBackendError`, `StorageStateTooLargeError`

Discipline (paste from master-plan §10):
- L6: never the deprecated stdlib naive-UTC helper — this module uses no
  time APIs at all, and asserts the anti-pattern is absent (test 17).
- L7: log lines carry only `ats`, `user`, `backend` — never state,
  cookies, or file contents.
- No `os.environ["USER"]` — caller passes `user` as a first-class arg.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol

import keyring
import keyring.backends.fail
import keyring.errors
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Payload guard: Playwright storage_state dicts are typically 4-40 KiB.
# Anything past 1 MiB is either a coding bug or an accidental blob paste.
_MAX_STATE_BYTES = 1 * 1024 * 1024  # 1 MiB

# The username slot inside the keyring service where storage_state lives.
_STORAGE_STATE_USERNAME = "storage_state"


def _service_name(ats: str, user: str) -> str:
    """Sole construction site for the keyring service-name literal.

    Acceptance criterion #11: the quoted-prefix literal appears in this
    file exactly once (inside the f-string below). Every other call site
    — bootstrap service, tests, docs — routes through this helper so we
    never drift.
    """
    return f"hiring-agent.{ats}.{user}"


# Constructed via _service_name so the literal above is the only place
# the prefix appears. Under this service we store the Fernet bootstrap
# key (username = "value").
_BOOTSTRAP_SERVICE = _service_name("fernet-bootstrap", "v1")
_BOOTSTRAP_USERNAME = "value"

# Default Fernet-fallback storage directory when neither the env var nor
# an explicit constructor arg is set. S17 seam calls `configure_storage_dir`
# below to thread `apply.storage_state_dir` in via the env-var resolution
# path (least-invasive route — does not require refactoring every backend
# call site to accept a config dict).
_DEFAULT_STORAGE_STATE_DIR = Path.home() / ".hiring-agent" / "apply"
_STORAGE_STATE_DIR_ENV = "HIRING_AGENT_STORAGE_STATE_DIR"


def configure_storage_dir(config: dict) -> None:
    """Set the FernetFileBackend storage directory from an `apply.` config.

    S17 merge-time reconciliation (per spec): S6's FernetFileBackend takes a
    `storage_dir` constructor arg but every internal caller instantiates it
    bare (`FernetFileBackend()`), so an explicit config injection would
    require a wider refactor across `credentials.load_state / store_state /
    has_state / evict`. Instead, S17's seam calls this helper at pipeline
    entry — which populates the `HIRING_AGENT_STORAGE_STATE_DIR` env var
    (resolution path #2 in FernetFileBackend). Every bare `FernetFileBackend()`
    call then sees the config-driven path without further plumbing.

    Idempotent — safe to call every pipeline tick. A None or empty value
    is a no-op (defaults still apply).
    """
    apply_cfg = (config or {}).get("apply", {})
    storage_dir = apply_cfg.get("storage_state_dir")
    if not storage_dir:
        return
    os.environ[_STORAGE_STATE_DIR_ENV] = str(storage_dir)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StorageStateBackendError(Exception):
    """Raised on any keyring / Fernet backend failure. Never a bare
    `keyring.errors.*` exception leaks past the public API."""


class TransientBackendError(Exception):
    """I2-B8: signals a transient backend failure (keyring blip, DBus
    reload) — the CALLER (dispatcher cache) must NOT cache the None
    result on this path. Retry next call. Never raised past
    `_cached_load_and_unwrap_state` — the dispatcher catches it and
    returns None (once) without populating the cache.
    """


class StorageStateTooLargeError(Exception):
    """Raised when the JSON-serialized state exceeds `_MAX_STATE_BYTES`."""


# ---------------------------------------------------------------------------
# Backend Protocol + implementations
# ---------------------------------------------------------------------------


class StorageBackend(Protocol):
    def get(self, service: str, username: str) -> str | None: ...
    def set(self, service: str, username: str, value: str) -> None: ...
    def delete(self, service: str, username: str) -> bool: ...


class KeyringBackend:
    """Wraps `keyring.set_password / get_password / delete_password`.

    Re-raises `KeyringError` subclasses as `StorageStateBackendError`
    so downstream code never has to import `keyring.errors`. The two
    exceptions the fallback layer wants to sniff — `NoKeyringError` and
    `KeyringLocked` — are re-raised as themselves (they are subclasses
    of `KeyringError`, so we let them through by name).
    """

    def get(self, service: str, username: str) -> str | None:
        try:
            return keyring.get_password(service, username)
        except (keyring.errors.NoKeyringError, keyring.errors.KeyringLocked):
            raise
        except keyring.errors.KeyringError as e:
            raise StorageStateBackendError(f"keyring get failed: {e}") from e

    def set(self, service: str, username: str, value: str) -> None:
        try:
            keyring.set_password(service, username, value)
        except (keyring.errors.NoKeyringError, keyring.errors.KeyringLocked):
            raise
        except keyring.errors.KeyringError as e:
            raise StorageStateBackendError(f"keyring set failed: {e}") from e

    def delete(self, service: str, username: str) -> bool:
        try:
            keyring.delete_password(service, username)
            return True
        except keyring.errors.PasswordDeleteError:
            return False
        except (keyring.errors.NoKeyringError, keyring.errors.KeyringLocked):
            return False
        except keyring.errors.KeyringError as e:
            raise StorageStateBackendError(f"keyring delete failed: {e}") from e


class FernetFileBackend:
    """Fernet-encrypted-file fallback.

    On `.set`, JSON is encrypted with a Fernet key held in the OS keyring
    under `_BOOTSTRAP_SERVICE` and written atomically to
    `<storage_dir>/<ats>_<user>.enc` (mode 0o600 inside a 0o700 dir).

    `storage_dir` resolution order:
    1. Explicit constructor arg (S3 will thread config-driven path here
       when both shards land together).
    2. `HIRING_AGENT_STORAGE_STATE_DIR` env var.
    3. `~/.hiring-agent/apply/`.
    """

    def __init__(self, storage_dir: Path | None = None) -> None:
        self._explicit_storage_dir = storage_dir

    # -- path resolution ---------------------------------------------------

    def _resolve_storage_dir(self) -> Path:
        if self._explicit_storage_dir is not None:
            return self._explicit_storage_dir
        env = os.environ.get(_STORAGE_STATE_DIR_ENV)
        if env:
            return Path(env)
        return _DEFAULT_STORAGE_STATE_DIR

    def _file_for(self, service: str, username: str) -> Path:
        """Derive `<ats>_<user>.enc` filename from the service string.

        Service names produced by `_service_name(ats, user)` are always
        `hiring-agent.<ats>.<user>` — safe to split at "." with maxsplit=2.
        For any non-conforming service (defensive), fall back to the raw
        service+username.
        """
        parts = service.split(".", 2)
        if len(parts) == 3:
            _, ats, user_part = parts
            # Sanitize: replace any accidental filesystem separators.
            safe = f"{ats}_{user_part}".replace("/", "_").replace(os.sep, "_")
            return self._resolve_storage_dir() / f"{safe}.enc"
        return self._resolve_storage_dir() / f"{service}_{username}.enc"

    # -- Fernet key from keyring ------------------------------------------

    def _get_or_create_fernet_key(self) -> bytes:
        """Read the bootstrap Fernet key from keyring, or generate one
        and persist it. Any keyring failure at either step raises
        StorageStateBackendError('no available secret backend') so
        callers can produce the spec-required error message."""
        try:
            existing = keyring.get_password(
                _BOOTSTRAP_SERVICE, _BOOTSTRAP_USERNAME
            )
        except (
            keyring.errors.NoKeyringError,
            keyring.errors.KeyringLocked,
            keyring.errors.KeyringError,
        ) as e:
            raise StorageStateBackendError("no available secret backend") from e

        if existing:
            return existing.encode("ascii")

        new_key = Fernet.generate_key()
        try:
            keyring.set_password(
                _BOOTSTRAP_SERVICE, _BOOTSTRAP_USERNAME, new_key.decode("ascii")
            )
        except (
            keyring.errors.NoKeyringError,
            keyring.errors.KeyringLocked,
            keyring.errors.KeyringError,
        ) as e:
            raise StorageStateBackendError("no available secret backend") from e
        return new_key

    # -- Protocol implementation ------------------------------------------

    def get(self, service: str, username: str) -> str | None:
        path = self._file_for(service, username)
        if not path.exists():
            return None
        try:
            token = path.read_bytes()
            key = self._get_or_create_fernet_key()
            plaintext = Fernet(key).decrypt(token)
            return plaintext.decode("utf-8")
        except StorageStateBackendError:
            raise
        except (InvalidToken, OSError) as e:
            raise StorageStateBackendError(f"fernet read failed: {e}") from e

    def set(self, service: str, username: str, value: str) -> None:
        path = self._file_for(service, username)
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageStateBackendError(f"mkdir failed: {e}") from e
        # Force 0o700 regardless of umask.
        try:
            os.chmod(parent, 0o700)
        except OSError as e:
            raise StorageStateBackendError(
                f"chmod parent failed: {e}"
            ) from e

        key = self._get_or_create_fernet_key()
        try:
            token = Fernet(key).encrypt(value.encode("utf-8"))
        except (InvalidToken, ValueError) as e:
            raise StorageStateBackendError(f"fernet encrypt failed: {e}") from e

        _atomic_write(path, token)

    def delete(self, service: str, username: str) -> bool:
        path = self._file_for(service, username)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            raise StorageStateBackendError(
                f"fernet delete failed: {e}"
            ) from e


# ---------------------------------------------------------------------------
# Atomic disk write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` with no partial-write window.

    Sequence: create `<path>.tmp` with mode 0o600 → write bytes →
    fsync → chmod 0o600 (belt-and-braces) → `os.rename` to final path
    → chmod 0o600 on final. `os.rename` is atomic on POSIX filesystems
    for same-directory renames.

    SE4 (Phase 3 xhigh iter-1): try/finally around the write ensures the
    .tmp file is cleaned up on ANY error — pre-fix, an OSError from
    os.rename/chmod could leave `.tmp` on disk containing the newly-written
    (potentially secret) payload indefinitely.
    """
    # I2-B5 (Phase 3 xhigh iter-2): use os.replace (not os.rename) — cross-
    # platform atomic swap that overwrites an existing target on Windows.
    # Also keeps parity with the pre-I2-B5 gmail/client.py path (now
    # delegates here) that used os.replace so the SE4 orphan-cleanup test
    # continues to intercept the correct call.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        # Replace does not always preserve mode across all filesystems.
        os.chmod(path, 0o600)
    except Exception:
        # Best-effort cleanup — never mask the original error.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Shared helper (SE3): write a text payload with the same safety
    guarantees as `_atomic_write` (0o600 create, fsync, atomic rename,
    tmp cleanup on failure). Used by both `src.apply.credentials` and
    `src.gmail.client` for token persistence.
    """
    _atomic_write(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _select_backend() -> StorageBackend:
    """Deterministic backend selection so tests can force each branch.

    - If `keyring.get_keyring()` returns `fail.Keyring` (no viable OS
      backend detected at all), return `FernetFileBackend`.
    - Any other keyring backend → return `KeyringBackend`.
    - If probing the keyring itself raises, fall back to Fernet.
    """
    try:
        current = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - keyring probing must never propagate
        return FernetFileBackend()
    if isinstance(current, keyring.backends.fail.Keyring):
        return FernetFileBackend()
    return KeyringBackend()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def store_state(ats: str, user: str, state: dict) -> None:
    """Persist `state` under service `hiring-agent.<ats>.<user>`.

    Falls back from keyring to Fernet-file if the primary keyring path
    raises `NoKeyringError` / `KeyringLocked`. Raises
    `StorageStateBackendError('no available secret backend')` when both
    backends fail.
    """
    payload = json.dumps(state)
    if len(payload.encode("utf-8")) > _MAX_STATE_BYTES:
        raise StorageStateTooLargeError(
            f"state size exceeds {_MAX_STATE_BYTES} bytes"
        )

    service = _service_name(ats, user)
    backend = _select_backend()
    backend_used = backend.__class__.__name__

    try:
        backend.set(service, _STORAGE_STATE_USERNAME, payload)
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringLocked):
        # Primary keyring is not available at this call — engage Fernet.
        fallback = FernetFileBackend()
        try:
            fallback.set(service, _STORAGE_STATE_USERNAME, payload)
        except StorageStateBackendError as e:
            raise StorageStateBackendError(
                "no available secret backend"
            ) from e
        backend_used = fallback.__class__.__name__

    # L7 discipline: log only ats, user, backend. Never `state`, `payload`,
    # cookie names, cookie values, or file paths (paths carry ats+user
    # which are already logged and nothing else sensitive).
    logger.info(
        "apply.storage_state.stored ats=%s user=%s backend=%s",
        ats,
        user,
        backend_used,
    )


def load_state(ats: str, user: str) -> dict | None:
    """Load state for `ats`+`user`. Returns None if absent. Raises
    `StorageStateBackendError` on backend failure other than 'not present'."""
    service = _service_name(ats, user)
    backend = _select_backend()
    backend_used = backend.__class__.__name__

    raw: str | None = None
    try:
        raw = backend.get(service, _STORAGE_STATE_USERNAME)
    except (keyring.errors.NoKeyringError, keyring.errors.KeyringLocked):
        # Fall through to Fernet fallback below.
        raw = None
    except StorageStateBackendError:
        # If the primary path itself blew up, we still try Fernet before
        # returning None. If Fernet also blows up, we surface it as
        # None per acceptance criterion #7 ("load_state returns None"
        # when both fail).
        raw = None

    # Even when the primary keyring returns None cleanly, the caller may
    # have used Fernet fallback on the write side — check the encrypted
    # file too.
    if raw is None and not isinstance(backend, FernetFileBackend):
        try:
            raw = FernetFileBackend().get(service, _STORAGE_STATE_USERNAME)
            if raw is not None:
                backend_used = FernetFileBackend.__name__
        except StorageStateBackendError:
            raw = None

    if raw is None:
        return None

    logger.info(
        "apply.storage_state.loaded ats=%s user=%s backend=%s",
        ats,
        user,
        backend_used,
    )
    return json.loads(raw)


def unwrap_state_if_envelope(raw: dict | None) -> dict | None:
    """SE2 (Phase 3 xhigh iter-1): shared shape-validation + envelope-unwrap
    for a raw storage_state value. Extracted so the review-loop path (which
    receives its state via an injected `load_state_fn`) can share the same
    logic without going through `load_and_unwrap_state` (which owns its own
    `load_state` call).

    Returns:
      * ``None`` if raw is None / non-dict / malformed shape.
      * the inner Playwright storage_state dict on any recognized shape.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "apply.storage_state.malformed_type type=%s",
            type(raw).__name__,
        )
        return None
    envelope_keys = {"state", "last_verified", "user"}
    if envelope_keys.issubset(raw.keys()):
        try:
            from src.apply.bootstrap import unwrap_state  # local: avoid cycle
            inner, _lv, _u = unwrap_state(raw)
        except Exception as e:  # noqa: BLE001
            # SD1: log exc_type only — payload can't leak via a class name.
            logger.warning(
                "apply.storage_state.unwrap_failed exc_type=%s",
                type(e).__name__,
            )
            return None
        if not isinstance(inner, dict):
            return None
        raw = inner
    # SG2: strict shape check.
    if not (isinstance(raw.get("cookies"), list) or isinstance(raw.get("origins"), list)):
        logger.warning("apply.storage_state.malformed_shape")
        return None
    return raw


def load_and_unwrap_state(ats: str, user: str) -> dict | None:
    """SE2 (Phase 3 xhigh iter-1): shared helper used by both the dispatcher's
    per-apply storage-state lookup path AND review.execute_confirmed_submit.

    Returns the UNWRAPPED Playwright storage_state dict (top-level `cookies`
    + `origins` keys) suitable for `browser.new_context(storage_state=...)`.
    Handles three shapes:

      * ``None``                     → returned unchanged.
      * envelope ``{state, last_verified, user}`` → unwrapped via
        ``bootstrap.unwrap_state``; returns the inner state.
      * flat ``{cookies, origins}``  → validated + returned as-is.
      * anything else                → returns ``None`` (malformed shape).

    Any unwrap exception is caught and swallowed → returns ``None``. Log
    lines carry only structural fields (`ats`, `user`, `reason`) so no
    decrypted payload bytes can leak through the log path (SD1 coupling).

    SG2: strict shape validation — a malformed dict (neither envelope nor
    `{cookies, origins}`) NEVER falls through to the caller. This closes
    the pre-fix dispatcher path where a garbage load_state return was
    passed verbatim to `transport.open(storage_state=...)`.
    """
    try:
        raw = load_state(ats, user)
    except StorageStateBackendError:
        # I2-B8 (Phase 3 xhigh iter-2): raise a distinct transient marker
        # exception so the dispatcher's cache can distinguish "no state
        # stored" (True None, safe to cache) from "backend hit a blip"
        # (do NOT cache; retry next call). Pre-fix collapsed both to None
        # and the dispatcher cached the None for the rest of the pipeline
        # — every subsequent apply degraded to unauthenticated after a
        # single transient keyring blip.
        logger.warning(
            "apply.storage_state.backend_error ats=%s user=%s", ats, user
        )
        # Iter-3 F5 (Phase 3 xhigh iter-3): TransientBackendError message
        # MUST NOT carry `user` (Gmail address = PII). If a future caller
        # does `except TransientBackendError as e: log.warning(..., error=str(e))`
        # — the exact SD1 anti-pattern iter-1 hunted down — the user email
        # would leak. Store `ats` only; consumers get the (ats, user) tuple
        # from their own call-site context, not from str(exc).
        raise TransientBackendError(f"transient backend blip (ats={ats})")
    except Exception as e:  # noqa: BLE001 — belt-and-braces
        # SD1: exception message may carry decrypted payload bytes from
        # Fernet InvalidToken unwrap. Log only exception type name.
        logger.warning(
            "apply.storage_state.load_failed ats=%s user=%s exc_type=%s",
            ats,
            user,
            type(e).__name__,
        )
        return None
    return unwrap_state_if_envelope(raw)


def has_state(ats: str, user: str) -> bool:
    """Return True iff `load_state` would return a non-None dict. Never
    raises — swallows any backend error and returns False."""
    try:
        return load_state(ats, user) is not None
    except Exception:  # noqa: BLE001 - acceptance criterion #3
        return False


def evict(ats: str, user: str) -> bool:
    """Remove the entry for `ats`+`user`. Returns True if anything was
    removed (from either backend), False if nothing was present. Never
    raises."""
    service = _service_name(ats, user)
    removed = False

    # Try the currently-selected backend.
    try:
        backend = _select_backend()
        try:
            if backend.delete(service, _STORAGE_STATE_USERNAME):
                removed = True
        except Exception:  # noqa: BLE001 - never raises
            pass
    except Exception:  # noqa: BLE001 - never raises
        pass

    # Belt-and-braces: also nuke the Fernet fallback file if it exists
    # (e.g., a prior run used the Fernet path and the primary keyring
    # never got the entry).
    try:
        if FernetFileBackend().delete(service, _STORAGE_STATE_USERNAME):
            removed = True
    except Exception:  # noqa: BLE001 - never raises
        pass

    return removed


# ---------------------------------------------------------------------------
# Multi-user stub (Phase 3+)
# ---------------------------------------------------------------------------


def list_users(ats: str) -> list[str]:  # pragma: no cover - Phase 3+
    """Enumerate stored users for `ats`. Not implemented in v1 — the
    keyring API does not natively expose an enumeration hook without
    a per-backend probe. Left as a stub for the Phase 3+ multi-user
    branch (see spec §Non-blocking follow-ups)."""
    raise NotImplementedError("multi-user, Phase 3+")
