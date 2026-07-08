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


def test_validate_apply_config_mkdir_targets_anchored_path_for_relative_dedup_db(
    tmp_path, monkeypatch
):
    """Regression: with a relative dedup_db_path and CWD elsewhere, the
    writability check + mkdir must target the REPO-ROOT anchored path — not
    the CWD-relative parent. Otherwise main.py checks/creates a different
    filesystem node than the one dedup.py actually opens against.

    Uses a Path.mkdir spy (no actual dir creation) so the assertion is
    hermetic — no repo-root pollution, no cleanup race, no ENOTEMPTY mask.
    """
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")
    monkeypatch.chdir(tmp_path)
    cfg = _valid_config(tmp_path)
    # Use a distinctive segment so it can't collide with any DIR_KEY mkdir
    # (which are absolute under tmp_path).
    cfg["apply"]["dedup_db_path"] = "dedup_leak/applied_jobs.db"  # RELATIVE

    # Spy Path.mkdir — record targets, do NOT actually create dirs. This is
    # the load-bearing assertion: fixed code passes the anchored parent to
    # mkdir; buggy code passes the CWD-relative parent.
    mkdir_calls: list[Path] = []

    def _spy_mkdir(self, *args, **kwargs):  # noqa: ANN001
        mkdir_calls.append(Path(self))

    monkeypatch.setattr(Path, "mkdir", _spy_mkdir)
    # Force writability check to pass on any path (the spy stubs mkdir but
    # os.access still runs; a nonexistent anchored parent walks up to the
    # repo root which IS writable, so this is defense-in-depth for hermeticity).
    monkeypatch.setattr("main.os.access", lambda p, mode: True)

    _validate_apply_config(cfg)

    anchored_parent = _anchor_at_repo_root("dedup_leak/applied_jobs.db").parent
    cwd_relative_parent = Path("dedup_leak")  # buggy Path(raw).parent shape

    # Fixed behavior: mkdir was called on the anchored (repo-root) parent.
    assert anchored_parent in mkdir_calls, (
        f"expected mkdir on anchored parent {anchored_parent}; "
        f"mkdir calls: {mkdir_calls}"
    )
    # Buggy behavior guard: mkdir must NOT have been called on the CWD-relative
    # parent — that's the split-brain smoking gun the fix closes.
    assert cwd_relative_parent not in mkdir_calls, (
        f"main.py leaked CWD-relative mkdir target {cwd_relative_parent}; "
        f"expected only anchored {anchored_parent}. mkdir calls: {mkdir_calls}"
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
