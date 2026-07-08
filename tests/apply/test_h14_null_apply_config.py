"""H14: _seam.initialize / run_for_job read apply_config.get("enabled",
False). If YAML has `apply: null` or `apply: false`, config.get("apply")
returns None/False → AttributeError on .get(). S3 validator soft-fails on
non-dict which propagates.

Fix: apply_config = config.get("apply") or {}; if not isinstance(...)
then {}.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_seam_handles_null_apply_section_in_yaml():
    """RED: config with apply=None must not crash the seam.

    Before H14: initialize() calls apply_config.get("enabled", False)
    on None → AttributeError.
    """
    from src.apply import _seam as seam_mod

    # apply is null (yaml `apply: null`).
    config_null = {"apply": None}
    # Should soft-noop and return [].
    events = seam_mod.initialize(config_null, gmail_client=MagicMock())
    assert events == []

    # apply is false (yaml `apply: false`).
    config_false = {"apply": False}
    events = seam_mod.initialize(config_false, gmail_client=MagicMock())
    assert events == []

    # apply key entirely missing.
    events = seam_mod.initialize({}, gmail_client=MagicMock())
    assert events == []


def test_seam_finalize_handles_null_apply_section():
    """finalize() must also handle a null apply section."""
    from src.apply import _seam as seam_mod
    # No exception should propagate.
    seam_mod.finalize({"apply": None})
    seam_mod.finalize({"apply": False})
    seam_mod.finalize({})


def test_seam_run_for_job_handles_null_apply_config():
    """run_for_job with apply_config=None or apply_config=False must
    soft-return None rather than raise AttributeError.
    """
    from src.apply import _seam as seam_mod
    from pathlib import Path

    # apply_config is None.
    result = seam_mod.run_for_job(
        job={},
        jd_text="",
        lane={},
        resume_path=None,
        cover_letter_path=None,
        apply_config=None,  # This is what a YAML `apply: null` yields.
        job_log=MagicMock(),
    )
    assert result is None

    # apply_config is False.
    result = seam_mod.run_for_job(
        job={},
        jd_text="",
        lane={},
        resume_path=None,
        cover_letter_path=None,
        apply_config=False,
        job_log=MagicMock(),
    )
    assert result is None
