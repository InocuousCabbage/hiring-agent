"""H10: navigation_retry / submit_no_retry decorators are defined but
NEVER APPLIED at any adapter usage site. Their invariants are unenforced.

Test is grep-based because the actual retry behavior is exhaustively
covered by test_retries.py — we just prove the decorators are present at
the expected navigation + submit call sites.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "apply" / "adapters"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_navigation_retry_applied_to_expected_methods():
    """RED: at least ONE @navigation_retry decorator must appear on a
    navigation-shaped method (page.goto / wait_for) in the greenhouse adapter.
    """
    src = _read(SRC / "greenhouse.py")
    assert "from src.apply.retries import" in src or "from src.apply import retries" in src, (
        "H10: retries module never imported in greenhouse.py"
    )
    assert "@navigation_retry" in src, (
        "H10: no navigation_retry decorator applied anywhere in greenhouse.py"
    )


def test_submit_no_retry_applied_to_submit_click():
    """RED: at least ONE @submit_no_retry decorator must mark the submit
    click path in greenhouse (or a helper that owns it).
    """
    src = _read(SRC / "greenhouse.py")
    assert "@submit_no_retry" in src, (
        "H10: no submit_no_retry marker applied to any submit call path"
    )


def test_computer_use_navigation_retry_or_documented_exemption():
    """computer_use adapter EITHER applies @navigation_retry to a
    navigation-shaped helper, OR documents the exemption in the module
    docstring. Present state (no decorator, no docstring exemption) is
    the H10 bug.
    """
    src = _read(SRC / "computer_use.py")
    has_decorator = "@navigation_retry" in src
    documented = "H10" in src and "computer_use" in src.lower() and "no navigation" in src.lower()
    assert has_decorator or documented, (
        "H10: computer_use has no @navigation_retry AND no H10 exemption note"
    )
