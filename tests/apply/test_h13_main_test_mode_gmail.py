"""H13: main.py --test branch calls run_pipeline WITHOUT gmail_client, so
poll_pending_reviews (after H2/H3) receives gmail=None → the fixed seam
now crashes on any Gmail method call during a --test invocation.

Fix: pass a gmail client (or a stub) even in --test mode.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

MAIN_SRC = Path(__file__).resolve().parents[2] / "src" / "main.py"


def _find_run_pipeline_calls(src: str) -> list[dict]:
    """Return {kwargs: [...], lineno: int} for each run_pipeline call."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "run_pipeline":
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                out.append({"kwargs": kwargs, "lineno": node.lineno})
    return out


def test_main_test_mode_passes_gmail_client_to_run_pipeline():
    """RED: every run_pipeline call in main.py must include gmail_client.

    Before H13: the --test branch omits gmail_client → gmail_client defaults
    to None → seam initialize() -> _call_poll_pending_reviews(gmail=None, ...)
    → poll_pending_reviews reads gmail.search(...) → AttributeError.
    """
    src = MAIN_SRC.read_text(encoding="utf-8")
    calls = _find_run_pipeline_calls(src)

    assert calls, "no run_pipeline calls found in main.py — parser regression"

    missing = [c for c in calls if "gmail_client" not in c["kwargs"]]
    assert not missing, (
        f"H13: run_pipeline invocation(s) missing gmail_client kwarg at "
        f"lines: {[c['lineno'] for c in missing]}"
    )
