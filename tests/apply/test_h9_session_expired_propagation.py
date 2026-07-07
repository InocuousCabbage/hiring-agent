"""H9: SessionExpiredError raised by an adapter must reach the seam's
except SessionExpiredError branch so notify_session_expired fires.

Before H4/H9, dispatcher's bare `except Exception` swallowed everything
and returned ApplyResult(status='failed'). The seam's except never fired.
"""
from __future__ import annotations

import sys
import types as pytypes
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _FakePage:
    url = ""
    def goto(self, url): self.url = url
    def close(self): pass


class _FakeTransport:
    @contextmanager
    def open(self, url, storage_state):
        class _S:
            page = _FakePage()
            replay_url = None
            transport = "local"
            proxies_enabled = False
            solve_captchas = False
        yield _S()


class _AdapterRaisingSessionExpired:
    name = "greenhouse"
    domains = ("boards.greenhouse.io",)

    def detect(self, url):
        return "greenhouse" in url

    def apply(self, page, ctx):
        from src.apply.base import SessionExpiredError
        raise SessionExpiredError(ats="greenhouse", last_run_iso=None)


def _install_fake_adapter(monkeypatch, adapter_cls):
    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []
    greenhouse_mod = pytypes.ModuleType("src.apply.adapters.greenhouse")
    greenhouse_mod.GreenhouseAdapter = adapter_cls
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", greenhouse_mod)


def test_dispatcher_reraises_session_expired_or_seam_triggers_notify(monkeypatch, tmp_path):
    """RED: with an adapter that raises SessionExpiredError, run_for_job
    must call notify_session_expired. Before the fix, dispatcher swallowed
    it into ApplyResult(status='failed') and the seam's except never fired.
    """
    _install_fake_adapter(monkeypatch, _AdapterRaisingSessionExpired)

    # Patch transport factory in dispatcher.
    import src.apply.transport as transport_mod
    monkeypatch.setattr(transport_mod, "get_transport", lambda config, kind: _FakeTransport())

    from src.apply import _seam as seam_mod

    notify_calls = []

    def fake_notify_session_expired(*, ats, user, last_run_iso, config):
        notify_calls.append({"ats": ats, "user": user, "last_run_iso": last_run_iso})

    monkeypatch.setattr(seam_mod, "_call_notify_session_expired", fake_notify_session_expired)

    from src.apply.profile import CandidateProfile
    from tests.fixtures.apply.profile_factory import load_example_profile

    # A minimal apply_config that makes the seam run.
    apply_config = {
        "enabled": True,
        "allowed_ats": ["greenhouse"],
        "profile_path": "templates/candidate_profile.yaml",
        "user": "jane",
        "mode": "review",
    }

    # Cache the real load result BEFORE patching so the patch doesn't recurse.
    _real_profile = load_example_profile()
    monkeypatch.setattr(
        "src.apply.profile.CandidateProfile.load",
        classmethod(lambda cls, path: _real_profile),
    )

    job = {"ats_apply_url": "https://boards.greenhouse.io/example/jobs/12345"}
    job_log = MagicMock()

    result = seam_mod.run_for_job(
        job=job,
        jd_text="Sample JD",
        lane={"name": "backend"},
        resume_path=tmp_path / "resume.pdf",
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=job_log,
    )

    # notify_session_expired must have been called.
    assert notify_calls, f"H9: notify_session_expired never fired; result={result}"
    assert notify_calls[0]["ats"] == "greenhouse"

    # And the result should be a 'skipped/session_expired' shape (per seam
    # contract).
    assert result is not None
    assert getattr(result, "status", None) == "skipped"
    assert getattr(result, "reason", None) == "session_expired"
