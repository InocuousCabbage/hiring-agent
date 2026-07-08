"""
tests/apply/test_credentials.py - Shard S6 storage-state-keyring RED tests.

Covers acceptance criteria 1-13 for src/apply/credentials.py per spec
`/Users/chiveschamoy/projects/hiring-agent/.agent/one-big-feature/
auto-apply-2026-07-06/03-specs/06-s6-storage-state-keyring.md`.

Isolation rule: NO test may touch the developer's real OS keyring.
`fake_keyring` fixture monkeypatches `keyring.set_password /
get_password / delete_password` to an in-memory dict. Every test that
exercises the code path uses either `fake_keyring` or a keyring-raising
patch. `storage_dir` fixture points the Fernet fallback at `tmp_path`
via env `HIRING_AGENT_STORAGE_STATE_DIR`.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import keyring
import keyring.backends.fail
import keyring.errors

# Ensure repo root is on sys.path so `import src.apply.credentials` works
# under `pytest tests/apply/test_credentials.py -v` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _InMemoryKeyring:
    """Fake keyring backend backing store, patched onto keyring.set_password
    et al. Never talks to the real OS keychain."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, username: str, value: str) -> None:
        self.store[(service, username)] = value

    def get(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete(self, service: str, username: str) -> None:
        key = (service, username)
        if key in self.store:
            del self.store[key]
        else:
            raise keyring.errors.PasswordDeleteError("not present")


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _InMemoryKeyring:
    fake = _InMemoryKeyring()
    monkeypatch.setattr(keyring, "set_password", fake.set)
    monkeypatch.setattr(keyring, "get_password", fake.get)
    monkeypatch.setattr(keyring, "delete_password", fake.delete)
    return fake


@pytest.fixture
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Fernet fallback at a per-test tmp dir via env var."""
    d = tmp_path / "storage_state"
    monkeypatch.setenv("HIRING_AGENT_STORAGE_STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _fresh_module_import():
    """Re-import the credentials module for each test so module-level
    caches (if any) do not leak state across tests."""
    for name in list(sys.modules):
        if name == "src.apply.credentials" or name.endswith(".apply.credentials"):
            del sys.modules[name]
    yield


# ---------------------------------------------------------------------------
# RED test 1 — service-name construction pattern
# ---------------------------------------------------------------------------


def test_service_name_pattern() -> None:
    from src.apply.credentials import _service_name

    assert _service_name("greenhouse", "ben") == "hiring-agent.greenhouse.ben"


# ---------------------------------------------------------------------------
# RED test 2 — keyring round-trip
# ---------------------------------------------------------------------------


def test_store_and_load_roundtrip_via_keyring(fake_keyring: _InMemoryKeyring) -> None:
    from src.apply.credentials import store_state, load_state

    state = {
        "cookies": [{"name": "session", "value": "abc123", "domain": ".example.com"}],
        "origins": [{"origin": "https://example.com", "localStorage": []}],
    }
    store_state("greenhouse", "ben", state)
    got = load_state("greenhouse", "ben")

    assert got is not None
    assert json.dumps(got, sort_keys=True) == json.dumps(state, sort_keys=True)


# ---------------------------------------------------------------------------
# RED test 3 — load returns None when absent
# ---------------------------------------------------------------------------


def test_load_state_returns_none_when_absent(
    fake_keyring: _InMemoryKeyring, storage_dir: Path
) -> None:
    from src.apply.credentials import load_state

    assert load_state("greenhouse", "ben") is None


# ---------------------------------------------------------------------------
# RED test 4 — has_state True after store
# ---------------------------------------------------------------------------


def test_has_state_true_after_store(fake_keyring: _InMemoryKeyring) -> None:
    from src.apply.credentials import store_state, has_state

    store_state("greenhouse", "ben", {"cookies": [], "origins": []})
    assert has_state("greenhouse", "ben") is True


# ---------------------------------------------------------------------------
# RED test 5 — has_state False when backend raises
# ---------------------------------------------------------------------------


def test_has_state_false_when_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.apply.credentials import has_state

    def boom(*args, **kwargs):
        raise RuntimeError("backend explosion")

    # Force load_state to raise via patching the primary backend's get()
    import src.apply.credentials as mod

    monkeypatch.setattr(mod.KeyringBackend, "get", boom)
    monkeypatch.setattr(mod.FernetFileBackend, "get", boom)

    # Must not propagate — returns False on any backend error.
    assert has_state("greenhouse", "ben") is False


# ---------------------------------------------------------------------------
# RED test 6 — evict returns True when present
# ---------------------------------------------------------------------------


def test_evict_returns_true_when_present(fake_keyring: _InMemoryKeyring) -> None:
    from src.apply.credentials import store_state, evict

    store_state("greenhouse", "ben", {"cookies": [], "origins": []})
    assert evict("greenhouse", "ben") is True


# ---------------------------------------------------------------------------
# RED test 7 — evict returns False when absent
# ---------------------------------------------------------------------------


def test_evict_returns_false_when_absent(
    fake_keyring: _InMemoryKeyring, storage_dir: Path
) -> None:
    from src.apply.credentials import evict

    assert evict("greenhouse", "nobody") is False


# ---------------------------------------------------------------------------
# RED test 8 — Fernet fallback engages on NoKeyringError
# ---------------------------------------------------------------------------


def test_backend_fallback_engages_on_no_keyring_error(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_password raises NoKeyringError for the storage_state entry;
    the bootstrap fernet-key entry still works (simulating a keyring where
    a specific write path is blocked but the whole keyring is not down)."""

    original_set = fake_keyring.set

    def selective_set(service: str, username: str, value: str) -> None:
        if username == "storage_state":
            raise keyring.errors.NoKeyringError("simulated: no primary")
        original_set(service, username, value)

    monkeypatch.setattr(keyring, "set_password", selective_set)

    from src.apply.credentials import store_state, load_state

    state = {"cookies": [{"name": "s", "value": "v"}], "origins": []}
    store_state("greenhouse", "ben", state)

    # Fernet file should exist under the storage_dir.
    assert storage_dir.exists()
    files = list(storage_dir.glob("*.enc"))
    assert len(files) == 1, f"expected 1 fernet file, saw: {files}"

    got = load_state("greenhouse", "ben")
    assert got == state


# ---------------------------------------------------------------------------
# RED test 9 — Fernet file perms 0o600
# ---------------------------------------------------------------------------


def test_fernet_file_permissions_are_0600(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.credentials as mod

    monkeypatch.setattr(
        mod, "_select_backend", lambda: mod.FernetFileBackend()
    )

    mod.store_state("greenhouse", "ben", {"cookies": [], "origins": []})
    files = list(storage_dir.glob("*.enc"))
    assert files, "fernet backend did not write a .enc file"
    mode = files[0].stat().st_mode & 0o777
    assert oct(mode) == "0o600", f"expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# RED test 10 — Fernet parent dir perms 0o700
# ---------------------------------------------------------------------------


def test_fernet_parent_dir_permissions_are_0700(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.credentials as mod

    monkeypatch.setattr(
        mod, "_select_backend", lambda: mod.FernetFileBackend()
    )

    mod.store_state("greenhouse", "ben", {"cookies": [], "origins": []})
    mode = storage_dir.stat().st_mode & 0o777
    assert oct(mode) == "0o700", f"expected 0o700, got {oct(mode)}"


# ---------------------------------------------------------------------------
# RED test 11 — atomic write uses .tmp then rename
# ---------------------------------------------------------------------------


def test_atomic_write_uses_tmp_then_rename(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.credentials as mod

    monkeypatch.setattr(
        mod, "_select_backend", lambda: mod.FernetFileBackend()
    )

    captured: list[tuple[str, str]] = []
    real_rename = os.rename

    def spy_rename(src, dst):
        captured.append((str(src), str(dst)))
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", spy_rename)

    mod.store_state("greenhouse", "ben", {"cookies": [], "origins": []})

    assert captured, "os.rename was never called"
    # There must be at least one rename call whose source ends with .tmp
    # and dest is the final .enc path.
    rename_calls_for_state = [
        (s, d) for (s, d) in captured if d.endswith(".enc")
    ]
    assert rename_calls_for_state, (
        f"no rename call landed on the final .enc path; saw: {captured}"
    )
    src, dst = rename_calls_for_state[-1]
    assert src.endswith(".tmp"), f"src did not end with .tmp: {src}"
    assert not dst.endswith(".tmp"), f"dst still ends with .tmp: {dst}"


# ---------------------------------------------------------------------------
# RED test 12 — StorageStateBackendError when both backends fail
# ---------------------------------------------------------------------------


def test_backend_error_raised_when_both_fail(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Universally raise NoKeyringError so neither the storage_state
    keyring path nor the fernet-bootstrap keyring path can succeed."""

    def always_raise(*args, **kwargs):
        raise keyring.errors.NoKeyringError("all backends down")

    monkeypatch.setattr(keyring, "set_password", always_raise)
    monkeypatch.setattr(keyring, "get_password", always_raise)

    from src.apply.credentials import store_state, StorageStateBackendError

    with pytest.raises(StorageStateBackendError):
        store_state("greenhouse", "ben", {"cookies": [], "origins": []})


# ---------------------------------------------------------------------------
# RED test 13 — payload > 1 MiB raises StorageStateTooLargeError
# ---------------------------------------------------------------------------


def test_too_large_state_raises(fake_keyring: _InMemoryKeyring) -> None:
    from src.apply.credentials import store_state, StorageStateTooLargeError

    huge = {"cookies": ["x" * 2_000_000]}
    with pytest.raises(StorageStateTooLargeError):
        store_state("greenhouse", "ben", huge)


# ---------------------------------------------------------------------------
# RED test 14 — no plaintext state / cookie values in log output (L7)
# ---------------------------------------------------------------------------


def test_no_plaintext_state_in_log_output(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.apply.credentials import store_state, load_state

    caplog.set_level("DEBUG")

    secret = "SECRET_COOKIE_VALUE_XYZ"
    state = {
        "cookies": [{"name": "session", "value": secret, "domain": ".x.com"}],
        "origins": [],
    }
    store_state("greenhouse", "ben", state)
    load_state("greenhouse", "ben")

    assert secret not in caplog.text, (
        "L7 violation: plaintext cookie value leaked into logs. "
        f"caplog.text preview: {caplog.text[:400]}"
    )


# ---------------------------------------------------------------------------
# RED test 15 — Fernet round-trip preserves state
# ---------------------------------------------------------------------------


def test_fernet_roundtrip_preserves_state(
    fake_keyring: _InMemoryKeyring,
    storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apply.credentials as mod

    monkeypatch.setattr(
        mod, "_select_backend", lambda: mod.FernetFileBackend()
    )

    state = {
        "cookies": [{"name": "session", "value": "abc123"}],
        "origins": [{"origin": "https://x", "localStorage": [{"n": "k", "v": "v"}]}],
    }
    mod.store_state("greenhouse", "ben", state)
    got = mod.load_state("greenhouse", "ben")

    assert got == state


# ---------------------------------------------------------------------------
# RED test 16 — service-name literal appears exactly once (only in
# _service_name)
# ---------------------------------------------------------------------------


def test_service_name_is_only_construction_site() -> None:
    import src.apply.credentials as mod

    src = Path(mod.__file__).read_text()
    # The grep pattern from the spec is `"hiring-agent\.` — literal double
    # quote followed by hiring-agent + dot. It must appear exactly once
    # (inside `_service_name`).
    hits = src.count('"hiring-agent.')
    hits += src.count("'hiring-agent.")  # cover single-quoted variant too
    assert hits == 1, (
        f'expected exactly 1 literal "hiring-agent." construction site, '
        f"found {hits}"
    )


# ---------------------------------------------------------------------------
# RED test 17 — no datetime.utcnow anywhere (L6)
# ---------------------------------------------------------------------------


def test_datetime_now_utc_used_if_any() -> None:
    import src.apply.credentials as mod

    src = Path(mod.__file__).read_text()
    assert "datetime.utcnow" not in src, (
        "L6 violation: datetime.utcnow() is deprecated in Python 3.12+; "
        "use datetime.now(timezone.utc)"
    )
