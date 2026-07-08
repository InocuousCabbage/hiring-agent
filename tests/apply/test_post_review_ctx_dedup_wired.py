"""Post-xhigh: ApplyContext must carry ctx.dedup so adapters can call
ctx.dedup.was_applied / count_today / soft_warn_check / record.

Before this fix, the frozen dataclass had no dedup field. Every adapter
call from the seam would AttributeError inside greenhouse.apply's gate 1
after H4 delivered a real page, then soft-fail to status='failed' —
crashing every production apply.
"""
from __future__ import annotations

import sys
import types as pytypes
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock


def test_apply_context_carries_dedup_field():
    """The ApplyContext dataclass must have a ``dedup`` field."""
    from src.apply.types import ApplyContext
    fields = getattr(ApplyContext, "__dataclass_fields__", {})
    assert "dedup" in fields, (
        "ApplyContext must expose a 'dedup' field so the seam can thread the "
        "DedupDB into the adapter (greenhouse.apply reads ctx.dedup)."
    )


def test_seam_populates_ctx_dedup(monkeypatch, tmp_path):
    """When the seam builds ApplyContext, ctx.dedup must be a DedupDB
    instance (not None) so the adapter's gates fire correctly.
    """
    captured_ctx = {}

    class _RecordingAdapter:
        name = "greenhouse"
        domains = ("boards.greenhouse.io",)
        def detect(self, url):
            return "greenhouse" in url
        def apply(self, page, ctx):
            captured_ctx["ctx"] = ctx
            from src.apply.types import ApplyResult
            return ApplyResult(status="review_required", ats=self.name)

    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []
    gh = pytypes.ModuleType("src.apply.adapters.greenhouse")
    gh.GreenhouseAdapter = _RecordingAdapter
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", gh)

    class _P:
        url = ""
        def goto(self, url): self.url = url
        def close(self): pass

    class _T:
        @contextmanager
        def open(self, url, storage_state):
            class S:
                page = _P()
                replay_url = None
                transport = "local"
                proxies_enabled = False
                solve_captchas = False
            yield S()

    import src.apply.transport as tm
    monkeypatch.setattr(tm, "get_transport", lambda cfg, kind: _T())

    from tests.fixtures.apply.profile_factory import load_example_profile
    _prof = load_example_profile()
    import src.apply.profile as pmod
    monkeypatch.setattr(pmod.CandidateProfile, "load",
                        classmethod(lambda cls, path: _prof))

    from src.apply import _seam as sm

    apply_config = {
        "enabled": True,
        "allowed_ats": ["greenhouse"],
        "profile_path": "x",
        "user": "jane",
        "mode": "review",
        "dedup_db_path": str(tmp_path / "dedup.db"),
    }

    sm.run_for_job(
        job={"ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1"},
        jd_text="JD",
        lane={"name": "backend"},
        resume_path=tmp_path / "resume.pdf",
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
    )

    ctx = captured_ctx.get("ctx")
    assert ctx is not None, "adapter.apply never received a ctx"
    assert getattr(ctx, "dedup", None) is not None, (
        "seam did not populate ctx.dedup — production adapters would crash "
        "when they read ctx.dedup.was_applied(...)"
    )
    # The dedup must expose the actual DedupDB surface.
    assert hasattr(ctx.dedup, "was_applied")
    assert hasattr(ctx.dedup, "count_today")
    assert hasattr(ctx.dedup, "record")
