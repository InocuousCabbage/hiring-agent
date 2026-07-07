"""RED tests for src.apply.dispatcher — S2 shard.

Contract per master-plan §4.1/4.2:
- `dispatch(url, config) -> ATSAdapter | None` — reads `apply.allowed_ats` on EVERY call (L14).
- `_ADAPTER_CLASSES: dict[str, str]` — string-map, not class objects (L12).
- `apply_to_job(url, ctx, config) -> ApplyResult` — soft-fails all adapter exceptions.
"""

from __future__ import annotations

import sys
import types as pytypes
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures — inject a fake `src.apply.adapters.greenhouse:GreenhouseAdapter`
# into sys.modules so the dispatcher's importlib call resolves it. This is
# the pattern that PROVES the registry is string-keyed (L12): the dispatcher
# never imported a class object at module-load time; it resolves at call
# time via importlib on the module string.
# ---------------------------------------------------------------------------

class _FakeGreenhouseAdapter:
    name = "greenhouse"
    domains = ("boards.greenhouse.io", "job-boards.greenhouse.io")

    def detect(self, url: str) -> bool:
        return any(d in url for d in self.domains)

    def apply(self, page, ctx):  # noqa: ARG002 — signature match only
        from src.apply.types import ApplyResult

        return ApplyResult(status="submitted", ats="greenhouse")


class _BoomAdapter:
    name = "greenhouse"
    domains = ("boards.greenhouse.io",)

    def detect(self, url: str) -> bool:
        return "greenhouse" in url

    def apply(self, page, ctx):  # noqa: ARG002
        raise RuntimeError("boom")


def _install_fake_adapter(monkeypatch, adapter_cls):
    """Wire a fake adapter class into src.apply.adapters.greenhouse."""
    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []  # mark as package
    greenhouse_mod = pytypes.ModuleType("src.apply.adapters.greenhouse")
    greenhouse_mod.GreenhouseAdapter = adapter_cls
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", greenhouse_mod)


def _sample_ctx():
    from src.apply.types import ApplyContext
    from tests.fixtures.apply.profile_factory import load_example_profile

    # S17 reconciliation: S2's inline 2-field CandidateProfile construction
    # was retired in favor of the S1 template loader (see
    # tests/fixtures/apply/profile_factory.py + S17 responsibility #4).
    return ApplyContext(
        profile=load_example_profile(),
        job={"url": "https://boards.greenhouse.io/example/jobs/12345"},
        resume_path=Path("/tmp/resume.pdf"),
        cover_letter_path=None,
        config={},
        applicant="jane",
        dry_run=True,
        mode="review",
    )


# ---------------------------------------------------------------------------
# dispatch() — URL → adapter resolution, gated by apply.allowed_ats (L14)
# ---------------------------------------------------------------------------

def test_dispatch_greenhouse_url_resolves_adapter(monkeypatch):
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import dispatch

    adapter = dispatch(
        "https://boards.greenhouse.io/example/jobs/12345",
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert adapter is not None
    assert adapter.name == "greenhouse"
    assert isinstance(adapter, _FakeGreenhouseAdapter)


def test_dispatch_respects_allowed_ats_gate(monkeypatch):
    """URL matches Greenhouse, but allowed_ats is empty → None (L14)."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import dispatch

    result = dispatch(
        "https://boards.greenhouse.io/example/jobs/12345",
        {"apply": {"allowed_ats": []}},
    )
    assert result is None


def test_dispatch_reads_allowed_ats_every_call(monkeypatch):
    """Mutating config between calls must be observed (no module-level cache) — L14."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import dispatch

    url = "https://boards.greenhouse.io/example/jobs/12345"
    config = {"apply": {"allowed_ats": []}}
    assert dispatch(url, config) is None

    config["apply"]["allowed_ats"] = ["greenhouse"]
    adapter = dispatch(url, config)
    assert adapter is not None
    assert adapter.name == "greenhouse"


def test_registry_uses_string_map_not_class_object(monkeypatch):
    """Patching src.apply.adapters.greenhouse.GreenhouseAdapter must be picked up
    by the dispatcher without touching dispatcher.py — proves L12."""
    from src.apply import dispatcher

    # The internal registry is a mapping of ATS name → 'module_path:class_name' string.
    assert isinstance(dispatcher._ADAPTER_CLASSES, dict)
    for key, val in dispatcher._ADAPTER_CLASSES.items():
        assert isinstance(key, str)
        assert isinstance(val, str), f"{key!r} maps to {val!r} — must be a string, not a class"
        assert ":" in val, f"{val!r} must be 'module_path:class_name'"

    # And the class the dispatcher instantiates is whatever is CURRENTLY at the module
    # attribute, not a captured reference.
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    adapter1 = dispatcher.dispatch(
        "https://boards.greenhouse.io/x/jobs/1",
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert isinstance(adapter1, _FakeGreenhouseAdapter)

    # Swap the module attribute; dispatcher must pick up the NEW class on the next call.
    _install_fake_adapter(monkeypatch, _BoomAdapter)
    adapter2 = dispatcher.dispatch(
        "https://boards.greenhouse.io/x/jobs/1",
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert isinstance(adapter2, _BoomAdapter)


# ---------------------------------------------------------------------------
# apply_to_job() — the public entry point
# ---------------------------------------------------------------------------

def test_apply_to_job_soft_fails_on_adapter_exception(monkeypatch):
    """Any exception raised inside adapter.apply MUST become status='failed'
    with reason='<ExcType>: <msg>' — never re-raise. (§4 + Q_BB1 addendum)"""
    _install_fake_adapter(monkeypatch, _BoomAdapter)
    from src.apply.dispatcher import apply_to_job

    ctx = _sample_ctx()
    result = apply_to_job(
        "https://boards.greenhouse.io/example/jobs/12345",
        ctx,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert result.status == "failed"
    assert result.reason == "RuntimeError: boom"


def test_apply_to_job_returns_skipped_when_no_adapter(monkeypatch):
    """URL matches no domain → ApplyResult(status='skipped', reason ~ 'no adapter')."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import apply_to_job

    ctx = _sample_ctx()
    result = apply_to_job(
        "https://careers.some-obscure-ats.com/jobs/999",
        ctx,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert result.status == "skipped"
    assert result.reason is not None
    assert "no adapter" in result.reason.lower()


def test_apply_to_job_returns_apply_result_on_success(monkeypatch):
    """Happy path: adapter returns a submitted result; apply_to_job passes it through."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import apply_to_job

    ctx = _sample_ctx()
    result = apply_to_job(
        "https://boards.greenhouse.io/example/jobs/12345",
        ctx,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert result.status == "submitted"
    assert result.ats == "greenhouse"


def test_apply_to_job_never_raises_on_missing_config(monkeypatch):
    """Config missing apply.allowed_ats → treated as empty; result is 'skipped', not exception."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    from src.apply.dispatcher import apply_to_job

    ctx = _sample_ctx()
    # No exception — soft-fail via 'skipped'.
    result = apply_to_job(
        "https://boards.greenhouse.io/example/jobs/12345",
        ctx,
        {},  # no 'apply' key at all
    )
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# S20 wire-through — long_tail=computer_use fallback dispatch
# ---------------------------------------------------------------------------


class _FakeComputerUseAdapter:
    name = "computer_use"
    domains = ()

    def detect(self, url: str) -> bool:  # noqa: ARG002 — catch-all
        return False

    def apply(self, page, ctx):  # noqa: ARG002 — signature match only
        from src.apply.types import ApplyResult

        return ApplyResult(status="review_required", ats="computer_use")


def _install_fake_computer_use(monkeypatch):
    """Wire a fake ComputerUseAdapter into src.apply.adapters.computer_use."""
    computer_use_mod = pytypes.ModuleType("src.apply.adapters.computer_use")
    computer_use_mod.ComputerUseAdapter = _FakeComputerUseAdapter
    monkeypatch.setitem(sys.modules, "src.apply.adapters.computer_use", computer_use_mod)


def test_dispatcher_falls_back_to_computer_use_when_long_tail_configured(monkeypatch):
    """Unmatched ATS URL + apply.long_tail='computer_use' → ComputerUseAdapter (S20).

    RED test written FIRST (per TDD skill chain, task §Reconciliation B).
    Verifies:
      - dispatch() returns a ComputerUseAdapter instance when no per-ATS match AND
        apply.long_tail == 'computer_use'.
      - Returns None when apply.long_tail is 'none' / missing.
      - The 'computer_use' key must be present in the string-map registry.
    """
    from src.apply import dispatcher

    # Contract: registry entry exists.
    assert "computer_use" in dispatcher._ADAPTER_CLASSES, \
        "computer_use must be registered as a long-tail fallback (S20)"

    _install_fake_computer_use(monkeypatch)

    unknown_url = "https://unknown-ats.example.test/apply/1"

    # long_tail='computer_use' + unmatched URL → ComputerUseAdapter
    adapter = dispatcher.dispatch(
        unknown_url,
        {"apply": {"allowed_ats": ["greenhouse"], "long_tail": "computer_use"}},
    )
    assert adapter is not None, "expected ComputerUseAdapter fallback, got None"
    assert isinstance(adapter, _FakeComputerUseAdapter)
    assert adapter.name == "computer_use"

    # long_tail='none' + unmatched URL → None
    adapter_none = dispatcher.dispatch(
        unknown_url,
        {"apply": {"allowed_ats": ["greenhouse"], "long_tail": "none"}},
    )
    assert adapter_none is None, "long_tail='none' must NOT trigger fallback"

    # Missing long_tail → None (default is 'none')
    adapter_default = dispatcher.dispatch(
        unknown_url,
        {"apply": {"allowed_ats": ["greenhouse"]}},
    )
    assert adapter_default is None, "missing long_tail must default to 'none'"


def test_dispatcher_computer_use_never_triggers_when_ats_matches(monkeypatch):
    """A URL that MATCHES a real ATS must not fall through to ComputerUseAdapter."""
    _install_fake_adapter(monkeypatch, _FakeGreenhouseAdapter)
    _install_fake_computer_use(monkeypatch)
    from src.apply.dispatcher import dispatch

    adapter = dispatch(
        "https://boards.greenhouse.io/example/jobs/12345",
        {"apply": {"allowed_ats": ["greenhouse"], "long_tail": "computer_use"}},
    )
    assert adapter is not None
    assert adapter.name == "greenhouse"
    assert isinstance(adapter, _FakeGreenhouseAdapter)


def test_validate_long_tail_allowlist():
    """validate_long_tail accepts {'none', 'computer_use'}; rejects any other value."""
    from src.apply.dispatcher import ConfigValidationError, validate_long_tail

    assert validate_long_tail("none") == "none"
    assert validate_long_tail("computer_use") == "computer_use"

    for bad in ("browser_use", "openai", "", "None", "COMPUTER_USE"):
        with pytest.raises(ConfigValidationError):
            validate_long_tail(bad)


# ---------------------------------------------------------------------------
# Public API exports (§Acceptance-criterion-1)
# ---------------------------------------------------------------------------

def test_public_api_exports_are_frozen():
    """`from src.apply import ...` must expose the frozen public surface."""
    from src.apply import (  # noqa: F401
        AdapterNotFoundError,
        ApplyContext,
        ApplyResult,
        ATSAdapter,
        FieldFill,
        SessionContext,
        Status,
        apply_to_job,
        dispatch,
    )
