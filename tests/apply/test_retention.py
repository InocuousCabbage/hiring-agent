"""S15: retention-rotation tests.

Written RED-first per TDD skill; the src/apply/retention.py module MUST NOT
exist when these are first run.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog
from structlog.testing import LogCapture

# Make repo root importable so `from src.apply.retention import ...` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# The retention_tree fixture module lives at tests/fixtures/apply/retention_tree.py.
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures" / "apply"))

from src.apply.retention import rotate, RotateResult  # noqa: E402
from retention_tree import seed_trace_tree  # noqa: E402


FIXED_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path: Path, retention_days: int | None = None) -> dict:
    apply_cfg: dict = {
        "trace_dir": str(tmp_path / "state" / "traces"),
        "screenshot_dir": str(tmp_path / "state" / "screenshots"),
    }
    if retention_days is not None:
        apply_cfg["retention_days"] = retention_days
    return {"apply": apply_cfg}


def _capture_logs():
    """Configure structlog with a fresh LogCapture and return it."""
    cap = LogCapture()
    structlog.configure(processors=[cap])
    return cap


# ---------------------------------------------------------------------------
# Acceptance-criteria tests (spec §Acceptance criteria + §TDD scaffolding)
# ---------------------------------------------------------------------------


def test_deletes_files_older_than_30_days(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[31, 1])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert not (trace_dir / "trace-31d.zip").exists()
    assert (trace_dir / "trace-1d.zip").exists()
    assert result.deleted_traces == 1
    assert result.deleted_screenshots == 0
    assert result.errors == 0


def test_missing_directory_is_no_op(tmp_path: Path) -> None:
    # Neither trace_dir nor screenshot_dir exists — this is the apply.enabled=false
    # / first-run case. Must not raise, must return zero counts.
    result = rotate(
        {
            "apply": {
                "retention_days": 30,
                "trace_dir": str(tmp_path / "nope-traces"),
                "screenshot_dir": str(tmp_path / "nope-shots"),
            }
        },
        now=FIXED_NOW,
    )
    assert result == RotateResult(0, 0, 0)


def test_default_retention_days_is_30(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[31, 29])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    # `retention_days` omitted → must default to 30.
    result = rotate(
        {
            "apply": {
                "trace_dir": str(trace_dir),
                "screenshot_dir": str(tmp_path / "state" / "screenshots"),
            }
        },
        now=FIXED_NOW,
    )

    assert not (trace_dir / "trace-31d.zip").exists()
    assert (trace_dir / "trace-29d.zip").exists()
    assert result.deleted_traces == 1


def test_custom_retention_days_7(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[8, 6])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    result = rotate(_cfg(tmp_path, 7), now=FIXED_NOW)

    assert not (trace_dir / "trace-8d.zip").exists()
    assert (trace_dir / "trace-6d.zip").exists()
    assert result.deleted_traces == 1


def test_cutoff_boundary_is_strict_less_than(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    trace_dir.mkdir(parents=True)
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    at_cutoff = trace_dir / "at-cutoff.zip"
    at_cutoff.write_bytes(b"")
    cutoff_ts = (FIXED_NOW - timedelta(days=30)).timestamp()
    os.utime(at_cutoff, (cutoff_ts, cutoff_ts))

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    # Strict `<`: a file exactly at the cutoff is kept.
    assert at_cutoff.exists()
    assert result.deleted_traces == 0


def test_permission_error_is_swallowed_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[100])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    def blocked_unlink(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError(f"denied: {self}")

    monkeypatch.setattr(Path, "unlink", blocked_unlink)

    # Must not raise; must count into `errors`.
    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert result.errors == 1
    assert result.deleted_traces == 0


def test_symlinks_are_skipped(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    trace_dir.mkdir(parents=True)
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    # Regular old file — must be deleted (control).
    real = trace_dir / "real-old.zip"
    real.write_bytes(b"")
    old_ts = (FIXED_NOW - timedelta(days=100)).timestamp()
    os.utime(real, (old_ts, old_ts))

    # Symlink to a file OUTSIDE the traces dir. The symlink must not be
    # deleted, and its target must not be followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    link_target = outside / "sensitive.txt"
    link_target.write_text("do-not-touch")
    os.utime(link_target, (old_ts, old_ts))

    sym = trace_dir / "link.zip"
    sym.symlink_to(link_target)

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert not real.exists()
    assert sym.is_symlink()  # symlink itself untouched
    assert link_target.exists()  # target not followed
    assert result.deleted_traces == 1
    assert result.errors == 0


def test_subdirs_recursed_one_level(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    session = trace_dir / "session-A"
    session.mkdir(parents=True)
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    old = session / "old.zip"
    old.write_bytes(b"")
    old_ts = (FIXED_NOW - timedelta(days=100)).timestamp()
    os.utime(old, (old_ts, old_ts))

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert not old.exists(), "nested one-level file should be deleted"
    assert session.exists(), "session directory itself must remain"
    assert result.deleted_traces == 1


def test_directories_are_never_removed(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    empty_old = trace_dir / "old-session"
    empty_old.mkdir(parents=True)
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    old_ts = (FIXED_NOW - timedelta(days=100)).timestamp()
    os.utime(empty_old, (old_ts, old_ts))

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert empty_old.exists(), "directories must never be removed"
    assert result.deleted_traces == 0


def test_screenshot_dir_rotated_alongside_traces(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    shot_dir = tmp_path / "state" / "screenshots"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[60], prefix="tr", ext=".zip")
    # Use distinct ages so each seeded file has a unique filename.
    seed_trace_tree(shot_dir, FIXED_NOW, ages_days=[60, 61], prefix="ss", ext=".png")

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert result.deleted_traces == 1
    assert result.deleted_screenshots == 2
    assert result.errors == 0


def test_summary_log_emitted_with_counts(tmp_path: Path) -> None:
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[60])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    cap = _capture_logs()
    try:
        rotate(_cfg(tmp_path, 30), now=FIXED_NOW)
    finally:
        structlog.reset_defaults()

    summaries = [e for e in cap.entries if e.get("event") == "retention.rotated"]
    assert len(summaries) == 1, cap.entries
    s = summaries[0]
    assert s["deleted_traces"] == 1
    assert s["deleted_screenshots"] == 0
    assert s["errors"] == 0
    assert s["retention_days"] == 30


def test_no_raw_path_in_error_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace_dir = tmp_path / "state" / "traces"
    # Filename carries a company-role-shaped substring that must NEVER bleed
    # into a log kv.
    seed_trace_tree(
        trace_dir,
        FIXED_NOW,
        ages_days=[100],
        prefix="acme-corp-role",
        ext=".zip",
    )
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    def blocked_unlink(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "unlink", blocked_unlink)

    cap = _capture_logs()
    try:
        rotate(_cfg(tmp_path, 30), now=FIXED_NOW)
    finally:
        structlog.reset_defaults()

    failed = [e for e in cap.entries if e.get("event") == "retention.delete_failed"]
    assert failed, "expected at least one retention.delete_failed event"
    for e in failed:
        # No kv value may contain the raw path substring.
        for k, v in e.items():
            assert "acme-corp-role" not in str(v), (
                f"raw path bled into kv {k!r}: {v!r}"
            )
        assert "path_hash" in e
        assert re.fullmatch(r"[0-9a-f]{12}", e["path_hash"]), (
            f"path_hash must be a 12-char sha256 prefix, got {e['path_hash']!r}"
        )


def test_file_not_found_during_unlink_is_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion #11: FileNotFoundError during unlink is a no-op —
    counted neither as delete nor as error. Simulates a concurrent cleaner
    that raced ahead of us between stat() and unlink()."""
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[100])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    def race_unlink(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise FileNotFoundError(str(self))

    monkeypatch.setattr(Path, "unlink", race_unlink)

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    # Neither counted as deleted nor as error.
    assert result.deleted_traces == 0
    assert result.errors == 0


def test_stat_permission_error_is_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an OSError raised by `stat()` on a would-be-fresh file
    must NOT count as an error and must NOT emit `retention.delete_failed`
    — no unlink was ever attempted, and spec §8 says "Absent errors, no
    other log lines"."""
    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[1])  # fresh — never eligible
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    orig_stat = Path.stat

    def blocked_stat(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        # Fail only on the seeded file; allow everything else so directory
        # walks and existence checks still work.
        if self.name == "trace-1d.zip":
            raise PermissionError("stat denied")
        return orig_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", blocked_stat)

    cap = _capture_logs()
    try:
        result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)
    finally:
        structlog.reset_defaults()

    assert result.deleted_traces == 0
    assert result.errors == 0
    # Only `retention.rotated` should appear — no per-file event.
    assert [e["event"] for e in cap.entries] == ["retention.rotated"]


def test_root_iterdir_error_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (code-review finding #1): OSError on the ROOT iterdir
    (e.g., `trace_dir` points at a regular file, or the dir is unreadable)
    must be COUNTED into `errors` — not just logged silently. Was previously
    asymmetric with the sub-dir iterdir handler that already counted."""
    trace_dir = tmp_path / "state" / "traces"
    trace_dir.mkdir(parents=True)
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    orig_iterdir = Path.iterdir

    def blocked_iterdir(self):  # noqa: ANN001
        # Fail only on the trace root; let everything else through.
        if self == trace_dir:
            raise PermissionError("iterdir denied")
        return orig_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", blocked_iterdir)

    result = rotate(_cfg(tmp_path, 30), now=FIXED_NOW)

    assert result.errors == 1, "root iterdir OSError must count into errors"
    assert result.deleted_traces == 0


def test_no_utcnow_in_source() -> None:
    src_path = _REPO_ROOT / "src" / "apply" / "retention.py"
    content = src_path.read_text()
    assert "utcnow" not in content, (
        "src/apply/retention.py must not use datetime.utcnow (landmine L6)"
    )


def test_now_default_is_call_time_not_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `now` were baked in at import time, rotate() called with no `now=`
    would use the import-time value and the file below (mtime = FIXED_NOW)
    would NOT be old enough relative to a future call-time `now`.

    We patch the module's `datetime` symbol so its call-time
    `datetime.now(timezone.utc)` returns a future timestamp; then a file
    at FIXED_NOW must be treated as older than 30 days.
    """
    import src.apply.retention as retention_mod

    trace_dir = tmp_path / "state" / "traces"
    seed_trace_tree(trace_dir, FIXED_NOW, ages_days=[0])
    (tmp_path / "state" / "screenshots").mkdir(parents=True)

    future_now = FIXED_NOW + timedelta(days=40)

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):  # noqa: ANN001
            return future_now

    monkeypatch.setattr(retention_mod, "datetime", _FakeDatetime)

    # No `now=` arg → module must resolve it inside the body.
    result = rotate(_cfg(tmp_path, 30))

    # cutoff = future_now - 30d = FIXED_NOW + 10d; file mtime = FIXED_NOW ⇒ deleted.
    assert result.deleted_traces == 1
    assert not (trace_dir / "trace-0d.zip").exists()
