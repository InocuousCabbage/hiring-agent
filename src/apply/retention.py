"""S15: retention-rotation.

Deterministic disk-hygiene routine invoked at the end of every pipeline run.
Deletes regular files older than `apply.retention_days` (default 30) from
`state/traces/` and `state/screenshots/`.

Design constraints (spec §Contracts consumed / §Code-review pass criteria):
- Standard library only — no `shutil` at all, no tree-delete.
- Directories are NEVER removed.
- Symlinks are skipped (never followed).
- Recursion is bounded to one level below each root — Playwright sometimes
  nests traces under a session dir, but deeper walks risk touching unrelated
  files.
- Cutoff is a strict `<`; a file with `mtime == cutoff` is kept.
- Always tz-aware `datetime.now(timezone.utc)` — never the deprecated
  tz-naive variant (landmine L6).
- Per-file errors log only `path_hash` — never the raw path (landmine L7,
  the filename can encode job-slug/company PII).
- Any error is swallowed; a locked file must never crash the pipeline.
"""

from __future__ import annotations

import hashlib
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

RotateResult = namedtuple(
    "RotateResult",
    ["deleted_traces", "deleted_screenshots", "errors"],
)

_log = structlog.get_logger(__name__)

_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_TRACE_DIR = "state/traces"
_DEFAULT_SCREENSHOT_DIR = "state/screenshots"


def _path_hash(path: Path) -> str:
    """Return a stable 12-char sha256 prefix for `path` — used in error kv."""
    return hashlib.sha256(str(path).encode()).hexdigest()[:12]


def _process_file(path: Path, cutoff_ts: float) -> tuple[int, int]:
    """Delete `path` if its mtime is strictly older than `cutoff_ts`.

    Returns `(deleted, errors)`. Any stat failure is a silent no-op — we
    never attempted a delete, so the summary contract "absent errors, no
    other log lines" (spec §8) forbids emitting `retention.delete_failed`
    here. Only the unlink call itself counts as a "delete" for error-
    accounting purposes.

    FileNotFoundError on unlink (races with a concurrent cleaner) is also
    a no-op — neither deleted nor error (spec §11).
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # Includes FileNotFoundError (race) and PermissionError (locked or
        # unreadable). Nothing to delete; nothing to count.
        return 0, 0

    if not (mtime < cutoff_ts):
        return 0, 0

    try:
        path.unlink()
    except FileNotFoundError:
        return 0, 0
    except OSError:  # includes PermissionError (subclass)
        _log.warning("retention.delete_failed", path_hash=_path_hash(path))
        return 0, 1
    return 1, 0


def _rotate_dir(root: Path, cutoff: datetime) -> tuple[int, int]:
    """Rotate one directory tree.

    Recurses at most ONE level: top-level files are eligible; if a top-level
    entry is a directory, its direct children are eligible; anything deeper
    is left alone. Directories are never removed. Symlinks — at either level —
    are skipped, not followed.

    Returns `(deleted_count, error_count)`.
    """
    if not root.exists():
        return 0, 0

    deleted = 0
    errors = 0
    cutoff_ts = cutoff.timestamp()

    try:
        top_children = list(root.iterdir())
    except OSError:
        # Root exists but is unreadable / not-a-directory. Count into errors
        # (spec §7 — errors are counted into the summary) and skip the walk.
        _log.warning("retention.delete_failed", path_hash=_path_hash(root))
        return 0, 1

    for child in top_children:
        if child.is_symlink():
            # Never follow — a stray symlink to ~/.ssh/id_rsa must not be
            # rotated by user error.
            continue
        if child.is_file():
            d, e = _process_file(child, cutoff_ts)
            deleted += d
            errors += e
            continue
        if child.is_dir():
            # Recurse EXACTLY one level.
            try:
                sub_children = list(child.iterdir())
            except OSError:
                _log.warning(
                    "retention.delete_failed", path_hash=_path_hash(child)
                )
                errors += 1
                continue
            for sub in sub_children:
                if sub.is_symlink():
                    continue
                if not sub.is_file():
                    # Do NOT recurse deeper; do NOT remove directories.
                    continue
                d, e = _process_file(sub, cutoff_ts)
                deleted += d
                errors += e
    return deleted, errors


def rotate(config: dict, now: datetime | None = None) -> RotateResult:
    """Delete regular files older than `apply.retention_days` from
    `apply.trace_dir` and `apply.screenshot_dir`.

    Args:
        config: full pipeline config; only `config["apply"]` is inspected.
        now: reference time used to compute the cutoff. `None` resolves to
            `datetime.now(timezone.utc)` AT CALL TIME (never at import time —
            landmine L6 and spec Acceptance criteria #1).

    Returns:
        `RotateResult(deleted_traces, deleted_screenshots, errors)`.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    apply_cfg = config.get("apply", {}) or {}
    retention_days = apply_cfg.get("retention_days", _DEFAULT_RETENTION_DAYS)
    trace_dir = Path(apply_cfg.get("trace_dir", _DEFAULT_TRACE_DIR))
    screenshot_dir = Path(
        apply_cfg.get("screenshot_dir", _DEFAULT_SCREENSHOT_DIR)
    )
    cutoff = now - timedelta(days=retention_days)

    deleted_traces, err_t = _rotate_dir(trace_dir, cutoff)
    deleted_shots, err_s = _rotate_dir(screenshot_dir, cutoff)
    errors = err_t + err_s

    result = RotateResult(deleted_traces, deleted_shots, errors)
    _log.info(
        "retention.rotated",
        deleted_traces=deleted_traces,
        deleted_screenshots=deleted_shots,
        errors=errors,
        retention_days=retention_days,
    )
    return result
