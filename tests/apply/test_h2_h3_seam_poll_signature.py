"""H2 + H3: _seam._call_poll_pending_reviews wires the poll wrong.

H2: passes (gmail=, now=, config=) but the real poll_pending_reviews signature
    is (gmail, store, now, config, *, adapter=None). Missing store positional
    + adapter kwarg → TypeError → swallowed by seam's bare except → poll
    silently returns [].

H3: passes ALREADY-UNWRAPPED apply_config; poll_pending_reviews does
    config["apply"].get("review_reping_hours") → KeyError('apply').
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_seam_poll_passes_store_and_adapter(tmp_path: Path, monkeypatch):
    """RED: _call_poll_pending_reviews must invoke poll_pending_reviews with
    the store positional AND an adapter kwarg. Before the fix, it passes
    neither and the call raises TypeError which the seam swallows.
    """
    from src.apply import _seam as seam_mod

    calls = {}

    def fake_poll(gmail, store, now, config, *, adapter=None):
        # Capture whether the real signature received both the positional
        # `store` and the `adapter` kwarg.
        calls["gmail"] = gmail
        calls["store"] = store
        calls["now"] = now
        calls["config"] = config
        calls["adapter"] = adapter
        return []

    # Patch the deferred import inside _call_poll_pending_reviews.
    import src.apply.review as review_mod
    monkeypatch.setattr(review_mod, "poll_pending_reviews", fake_poll)

    # Give the seam a config that exercises the wrap (H3 checks the shape
    # separately; here we only need enabled=True and the dedup_db_path set).
    config = {
        "apply": {
            "enabled": True,
            "gmail_label_prefix": "hiring-agent/apply",
            "dedup_db_path": str(tmp_path / "review.db"),
            "review_reping_hours": 24,
            "review_timeout_hours": 72,
        }
    }
    apply_cfg = config["apply"]

    result = seam_mod._call_poll_pending_reviews(
        gmail=MagicMock(),
        now=datetime.now(timezone.utc),
        config=apply_cfg,
    )
    assert result == []
    # Must have supplied a real ReviewStore (not None) and left adapter=None
    # or set the kwarg — either way, the CALL must not have blown up on
    # missing positional args.
    assert "store" in calls, "poll_pending_reviews never received a store positional"
    assert calls["store"] is not None
    from src.apply.state_store import ReviewStore
    assert isinstance(calls["store"], ReviewStore)
    # `adapter` may be None but the kwarg must have been reachable.
    assert "adapter" in calls


def test_seam_poll_receives_wrapped_config(tmp_path: Path, monkeypatch):
    """RED: poll_pending_reviews reads config["apply"].get(...); the seam must
    pass the WRAPPED config, not the already-unwrapped inner dict. Before the
    fix it passes apply_config directly → KeyError('apply').
    """
    from src.apply import _seam as seam_mod

    received_config = {}

    def fake_poll(gmail, store, now, config, *, adapter=None):
        # Simulate the real poller's access pattern.
        received_config["config"] = config
        received_config["reping_hours"] = int(
            config["apply"].get("review_reping_hours", 24)
        )
        return []

    import src.apply.review as review_mod
    monkeypatch.setattr(review_mod, "poll_pending_reviews", fake_poll)

    inner_apply_cfg = {
        "enabled": True,
        "gmail_label_prefix": "hiring-agent/apply",
        "dedup_db_path": str(tmp_path / "review.db"),
        "review_reping_hours": 6,
        "review_timeout_hours": 72,
    }
    result = seam_mod._call_poll_pending_reviews(
        gmail=MagicMock(),
        now=datetime.now(timezone.utc),
        config=inner_apply_cfg,
    )
    assert result == []
    # If H3 is fixed, the poller's config["apply"].get("review_reping_hours")
    # returned 6 (from our inner config). Before the fix it would KeyError on
    # "apply".
    assert received_config["reping_hours"] == 6
