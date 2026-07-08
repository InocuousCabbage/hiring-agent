"""RED tests: main.py's dedup_db_path writability check must route through the
SAME `_anchor_at_repo_root` helper that `src/apply/dedup.py` uses.

Bug (pre-fix): `src/main.py::_validate_apply_config` did
    ddp = Path(apply_cfg["dedup_db_path"])
    ddp_anchor = _writable_ancestor(ddp.parent)
    os.access(ddp_anchor, os.W_OK)
    ...
    ddp.parent.mkdir(parents=True, exist_ok=True)

That naive `Path(...)` is CWD-relative. So the pipeline could log "dedup DB
writable at <CWD>/state" while `dedup.py` silently opens `<repo>/state/…`.
Split-brain — the writability precheck was checking a different filesystem
node than the one that actually gets opened.

Sibling of PR #5. Post-MVP hardening PR 4.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from main import _dedup_db_writability_target, _validate_apply_config  # noqa: E402
from apply.dedup import _anchor_at_repo_root  # noqa: E402

# Re-use the valid-config fixture shape from test_config_gate.py to keep the
# RED tests focused on THIS regression (dedup_db_path resolution) rather than
# re-litigating every field validator.
from tests.apply.test_config_gate import _valid_config  # noqa: E402


# ── Helper: unit-test _dedup_db_writability_target directly ─────────────────


def test_dedup_writability_target_anchors_relative_at_repo_root(tmp_path, monkeypatch):
    """A relative dedup_db_path resolves through _anchor_at_repo_root — same
    helper dedup.py uses — so main.py's writability check targets the repo
    root anchor, NOT the CWD-relative parent."""
    monkeypatch.chdir(tmp_path)  # simulate a foreign CWD
    target = _dedup_db_writability_target("state/applied_jobs.db")
    expected = _anchor_at_repo_root("state/applied_jobs.db").parent
    assert target == expected, (
        f"main.py must check the anchored parent {expected}, got {target}. "
        f"Naive Path(...) would resolve to a CWD-relative parent."
    )
    # Sanity: anchored parent must NOT leak the CWD.
    assert str(tmp_path) not in str(target), (
        f"target leaked CWD {tmp_path}: {target}"
    )


def test_dedup_writability_target_preserves_absolute_path(tmp_path):
    """Absolute config values pass through unchanged (no repo-root prepend)."""
    abs_path = tmp_path / "custom" / "dedup.db"
    target = _dedup_db_writability_target(str(abs_path))
    assert target == abs_path.parent


def test_dedup_writability_target_skips_memory_uri():
    """`:memory:` is a SQLite non-filesystem spec — nothing to writability
    check on disk. Helper must return None (skip)."""
    assert _dedup_db_writability_target(":memory:") is None


def test_dedup_writability_target_skips_file_uri():
    """`file:...` is a SQLite URI (e.g. shared-cache in-memory DB) —
    nothing to writability check on disk. Helper must return None."""
    assert _dedup_db_writability_target("file::memory:?cache=shared") is None


# ── Integration: _validate_apply_config end-to-end regression ───────────────


def test_validate_apply_config_no_mkdir_leak_into_cwd_for_relative_dedup_db(
    tmp_path, monkeypatch
):
    """Regression: with a relative dedup_db_path and CWD elsewhere, the
    writability check + mkdir must NOT leak a `state/` directory into the
    CWD. Naive CWD-relative resolution would create <CWD>/state/ — the
    split-brain smoking gun."""
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")
    monkeypatch.chdir(tmp_path)
    cfg = _valid_config(tmp_path)
    cfg["apply"]["dedup_db_path"] = "state/applied_jobs.db"  # RELATIVE

    _validate_apply_config(cfg)

    # Buggy code would have mkdir'd <tmp_path>/state/ (CWD-relative). The fix
    # anchors at repo root, so no CWD leakage.
    assert not (tmp_path / "state").exists(), (
        f"dedup_db_path mkdir leaked into CWD {tmp_path}/state — writability "
        f"check + mkdir must route through _anchor_at_repo_root."
    )

    # Positive assertion: the anchored parent (repo-root state/) exists.
    anchored_parent = _anchor_at_repo_root("state/applied_jobs.db").parent
    assert anchored_parent.exists(), (
        f"expected anchored parent {anchored_parent} to exist after validate"
    )


def test_validate_apply_config_accepts_memory_dedup_db(tmp_path, monkeypatch):
    """`:memory:` dedup_db_path must validate cleanly — no writability check,
    no mkdir, no ConfigError. Even with CWD changes, the SQLite special path
    is passthrough."""
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")
    monkeypatch.chdir(tmp_path)
    cfg = _valid_config(tmp_path)
    cfg["apply"]["dedup_db_path"] = ":memory:"

    # Must not raise. Must not create any dir literally named ':memory:'.
    _validate_apply_config(cfg)
    assert not (tmp_path / ":memory:").exists()
    assert not (ROOT / ":memory:").exists()


def test_validate_apply_config_accepts_file_uri_dedup_db(tmp_path, monkeypatch):
    """`file:...` URI dedup_db_path must validate cleanly — no writability
    check, no mkdir, no ConfigError."""
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")
    monkeypatch.chdir(tmp_path)
    cfg = _valid_config(tmp_path)
    cfg["apply"]["dedup_db_path"] = "file::memory:?cache=shared"

    # Must not raise. Must not create any file/dir with that URI as its name.
    _validate_apply_config(cfg)
    assert not (tmp_path / "file::memory:?cache=shared").exists()
