"""H7: _seam.finalize reads getattr(result, 'rotated_count', 0) but
RotateResult is namedtuple(deleted_traces, deleted_screenshots, errors) —
no rotated_count field. Log always emits rotated=0.
"""
from __future__ import annotations

from collections import namedtuple
from unittest.mock import patch

import structlog.testing

from src.apply import _seam as seam_mod
from src.apply.retention import RotateResult


def test_seam_retention_log_emits_actual_deletion_count(monkeypatch):
    """RED: mock rotate to return RotateResult(3, 2, 0); the log record
    for apply.retention.rotated must show the sum (5), not 0.
    """
    def fake_rotate(config, now=None):
        return RotateResult(deleted_traces=3, deleted_screenshots=2, errors=0)

    import src.apply.retention as retention_mod
    monkeypatch.setattr(retention_mod, "rotate", fake_rotate)

    config = {"apply": {"enabled": True}}

    with structlog.testing.capture_logs() as captured:
        seam_mod.finalize(config)

    # Find the retention rotated event.
    rotated = [e for e in captured if e.get("event") == "apply.retention.rotated"]
    assert rotated, f"expected apply.retention.rotated log; captured events: {[e.get('event') for e in captured]}"
    ev = rotated[0]
    # Before the fix: ev['rotated'] == 0.
    # After the fix: total = 3 + 2 = 5.
    total = ev.get("rotated", 0)
    assert total == 5, f"H7: expected total 5 (traces=3 + screenshots=2), got {total}"
