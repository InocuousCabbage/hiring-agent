"""H11: dispatcher long_tail=computer_use loads the adapter even when
'computer_use' is NOT in apply.allowed_ats. Contradicts L14 (allowed_ats
is the gate for adapter selection).

Fix: only reach the computer_use fallback if 'computer_use' membership is
in apply.allowed_ats.
"""
from __future__ import annotations

import sys
import types as pytypes

import pytest


class _FakeComputerUseAdapter:
    name = "computer_use"
    domains = ()

    def detect(self, url):
        return True

    def apply(self, page, ctx):
        from src.apply.types import ApplyResult
        return ApplyResult(status="review_required", ats="computer_use")


def _install_computer_use(monkeypatch):
    mod = pytypes.ModuleType("src.apply.adapters.computer_use")
    mod.ComputerUseAdapter = _FakeComputerUseAdapter
    monkeypatch.setitem(sys.modules, "src.apply.adapters.computer_use", mod)


def test_long_tail_respects_allowed_ats_membership(monkeypatch):
    """RED: with allowed_ats=['greenhouse'] and long_tail='computer_use',
    an unmatched URL must NOT fall back to computer_use — the fallback
    adapter is not a member of the allowlist.
    """
    _install_computer_use(monkeypatch)
    from src.apply.dispatcher import dispatch

    # long_tail says 'computer_use', but allowed_ats does NOT include it.
    config = {
        "apply": {
            "allowed_ats": ["greenhouse"],
            "long_tail": "computer_use",
        }
    }
    adapter = dispatch("https://careers.some-random-ats.com/jobs/1", config)
    assert adapter is None, (
        f"H11: fallback fired despite computer_use not in allowed_ats; "
        f"got adapter={adapter}"
    )


def test_long_tail_fires_when_computer_use_in_allowed_ats(monkeypatch):
    """Verify the happy path: when the operator explicitly lists
    'computer_use' in allowed_ats AND sets long_tail='computer_use', the
    fallback DOES fire on an unmatched URL.
    """
    _install_computer_use(monkeypatch)
    from src.apply.dispatcher import dispatch

    config = {
        "apply": {
            "allowed_ats": ["greenhouse", "computer_use"],
            "long_tail": "computer_use",
        }
    }
    adapter = dispatch("https://careers.some-random-ats.com/jobs/1", config)
    assert adapter is not None, "H11 regression: fallback should fire when allowed_ats includes computer_use"
    assert adapter.name == "computer_use"
