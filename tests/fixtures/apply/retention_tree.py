"""Fixture helper for S15 retention tests.

Seeds a `tmp_path`-style directory with files whose mtime is `now - age_days`
for each entry in `ages_days`. Returns the created paths.

Intentionally standard-library only.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


def seed_trace_tree(
    root: Path,
    now: datetime,
    ages_days: Iterable[int],
    ext: str = ".zip",
    prefix: str = "trace",
) -> list[Path]:
    """Create `root` and drop files inside whose mtime is `now - age_days`.

    Args:
        root: directory to create + populate.
        now: reference "now" datetime; mtime is `now - timedelta(days=age)`.
        ages_days: iterable of integer ages, one file per entry.
        ext: file extension, including the leading dot.
        prefix: filename prefix; the file will be `{prefix}-{age}d{ext}`.

    Returns:
        List of created file paths in iteration order of `ages_days`.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for age in ages_days:
        path = root / f"{prefix}-{age}d{ext}"
        path.write_bytes(b"")
        mtime_ts = (now - timedelta(days=age)).timestamp()
        os.utime(path, (mtime_ts, mtime_ts))
        paths.append(path)
    return paths
