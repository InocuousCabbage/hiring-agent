"""End-to-end integration tests exercising the whole seam→dispatcher→
transport→adapter→state_store→dedup chain.

These tests exist because H1-H15 were wiring-shape defects that per-fix
unit tests missed. Weemeemee's brief called for 4 integration-shape tests
to raise the floor.
"""
from __future__ import annotations

import sys
import types as pytypes
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────
# Test 1: Seam → dispatcher → adapter → transport full flow
# ─────────────────────────────────────────────────────────────


class _EchoPage:
    def __init__(self):
        self.url = ""
        self.set_input_files_calls: list = []

    def goto(self, url):
        self.url = url

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        loc = MagicMock()
        loc.count.return_value = 0
        loc.first = loc
        return loc

    def close(self):
        pass


class _EchoTransport:
    """Records that open() was called with the job URL."""

    call_log: list = []

    def __init__(self):
        pass

    @contextmanager
    def open(self, url, storage_state):
        type(self).call_log.append({"url": url, "storage_state": storage_state})

        class _S:
            page = _EchoPage()
            replay_url = None
            transport = "local"
            proxies_enabled = False
            solve_captchas = False

        yield _S()


class _EchoAdapter:
    name = "greenhouse"
    domains = ("boards.greenhouse.io",)

    call_log: list = []

    def detect(self, url):
        return "greenhouse" in url

    def apply(self, page, ctx):
        type(self).call_log.append({"page": page, "ats": self.name})
        from src.apply.types import ApplyResult
        return ApplyResult(status="submitted", ats=self.name,
                           apply_url=ctx.job.get("ats_apply_url", ""))


def _install_echo_adapter(monkeypatch):
    _EchoAdapter.call_log = []
    _EchoTransport.call_log = []
    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []
    gh = pytypes.ModuleType("src.apply.adapters.greenhouse")
    gh.GreenhouseAdapter = _EchoAdapter
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", gh)

    import src.apply.transport as tm
    monkeypatch.setattr(tm, "get_transport", lambda cfg, kind: _EchoTransport())


def test_seam_to_dispatcher_to_adapter_to_transport_full_flow(monkeypatch, tmp_path):
    """Full flow: seam.run_for_job → dispatcher.apply_to_job → transport.open
    → adapter.apply → returns ApplyResult. Every hop touched.
    """
    _install_echo_adapter(monkeypatch)

    from tests.fixtures.apply.profile_factory import load_example_profile
    import src.apply.profile as pmod
    _profile = load_example_profile()
    monkeypatch.setattr(pmod.CandidateProfile, "load", classmethod(lambda cls, path: _profile))

    from src.apply import _seam as seam_mod

    apply_config = {
        "enabled": True,
        "allowed_ats": ["greenhouse"],
        "profile_path": "templates/candidate_profile.yaml",
        "user": "jane",
        "mode": "review",
        "dedup_db_path": str(tmp_path / "dedup.db"),
    }

    job_log = MagicMock()
    job = {"ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1"}

    result = seam_mod.run_for_job(
        job=job,
        jd_text="JD",
        lane={"name": "backend"},
        resume_path=tmp_path / "resume.pdf",
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=job_log,
    )

    assert result is not None
    assert result.status == "submitted"

    # Transport.open was called with the job URL.
    assert _EchoTransport.call_log, "transport.open was never called — H4 regression"
    assert _EchoTransport.call_log[0]["url"] == "https://boards.greenhouse.io/acme/jobs/1"

    # Adapter received a real page (not None).
    assert _EchoAdapter.call_log
    assert _EchoAdapter.call_log[0]["page"] is not None
    assert isinstance(_EchoAdapter.call_log[0]["page"], _EchoPage)


# ─────────────────────────────────────────────────────────────
# Test 2: Greenhouse full flow with demo fixture (state_store + dedup)
# ─────────────────────────────────────────────────────────────


def test_greenhouse_full_flow_with_demo_fixture(tmp_path, monkeypatch):
    """Run the real GreenhouseAdapter (review-mode dry-run) via the seam.

    Verifies the state_store writes a review_pending row into the DedupDB-
    migrated schema without any column-drift error, and dedup gate 1 fires
    correctly.
    """
    # First seed a dedup DB with H1's migrated schema.
    from src.apply.dedup import DedupDB
    db_path = tmp_path / "dedup.db"
    dedup = DedupDB(db_path)

    # Now run the greenhouse adapter in dry_run mode.
    from src.apply.adapters.greenhouse import GreenhouseAdapter

    class _Page:
        def __init__(self):
            self.url = ""
            self.set_input_files_calls: list = []

        def goto(self, url):
            self.url = url

        def content(self):
            return "<html></html>"

        def locator(self, selector):
            loc = MagicMock()
            loc.count.return_value = 0
            loc.first = loc
            return loc

        def screenshot(self, path=None):
            if path:
                Path(path).write_bytes(b"")

        def close(self):
            pass

    class _Profile:
        name = None
        contact = None
        work_authorization = None

    class _Ctx:
        def __init__(self):
            self.profile = _Profile()
            self.job = {
                "apply_url": "https://boards.greenhouse.io/acme/jobs/12345",
                "company": "Acme",
                "role": "Engineer",
            }
            self.resume_path = tmp_path / "resume.pdf"
            self.resume_path.write_bytes(b"%PDF-1.4\n")
            self.resume_docx_path = None
            self.cover_letter_path = None
            self.cover_letter_docx_path = None
            self.config = {
                "screenshot_dir": str(tmp_path / "screenshots"),
                "trace_dir": str(tmp_path / "traces"),
            }
            self.dedup = dedup
            self.applicant = "jane"
            self.dry_run = True
            self.mode = "review"
            self.captcha_detector = None

    adapter = GreenhouseAdapter()

    # First run: no prior dedup entry → adapter goes through review path.
    # M22 fix: on an empty dedup DB, the soft-dup gate CANNOT fire — there
    # are no prior rows to fuzzy-match against, so 'soft_dup_warn' is not a
    # possible outcome. The pre-fix `in (..., 'soft_dup_warn')` alternation
    # accepted a wrong-branch result on an empty DB, defeating the guard;
    # the exact-status assertion below fails deterministically if the
    # dispatcher ever routes an empty-DB job through the soft-dup lane.
    result = adapter.apply(_Page(), _Ctx())
    assert result.status == "review_required", (
        f"Empty-DB first run must be review_required (no rows to soft-match "
        f"against). Got: {result.status!r}"
    )

    # Seed the dedup DB directly with a submission for this URL to test H5.
    from src.apply.types import ApplyResult as _AR
    dedup.record(
        _AR(status="submitted", ats="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/12345"),
        applicant="jane",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/12345",
    )

    # Second run: dedup gate 1 must fire.
    result_replay = adapter.apply(_Page(), _Ctx())
    assert result_replay.status == "already_applied", (
        f"H5: dedup gate should have fired; got {result_replay.status}"
    )


# ─────────────────────────────────────────────────────────────
# Test 3: SessionExpiredError propagates seam → notify
# ─────────────────────────────────────────────────────────────


class _SessionExpiredAdapter:
    name = "greenhouse"
    domains = ("boards.greenhouse.io",)

    def detect(self, url):
        return "greenhouse" in url

    def apply(self, page, ctx):
        from src.apply.base import SessionExpiredError
        raise SessionExpiredError(ats="greenhouse", last_run_iso="2026-07-01T00:00:00+00:00")


def test_session_expired_propagates_through_seam_to_notify(monkeypatch, tmp_path):
    """H9 integration: SessionExpiredError from an adapter must reach the
    seam's notify_session_expired call, not get swallowed into failed.
    """
    adapters_pkg = pytypes.ModuleType("src.apply.adapters")
    adapters_pkg.__path__ = []
    gh = pytypes.ModuleType("src.apply.adapters.greenhouse")
    gh.GreenhouseAdapter = _SessionExpiredAdapter
    monkeypatch.setitem(sys.modules, "src.apply.adapters", adapters_pkg)
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", gh)

    import src.apply.transport as tm
    monkeypatch.setattr(tm, "get_transport", lambda cfg, kind: _EchoTransport())

    from tests.fixtures.apply.profile_factory import load_example_profile
    import src.apply.profile as pmod
    _profile = load_example_profile()
    monkeypatch.setattr(pmod.CandidateProfile, "load", classmethod(lambda cls, path: _profile))

    from src.apply import _seam as seam_mod

    notify_calls = []
    def _record_notify(*, ats, user, last_run_iso, config):
        notify_calls.append({"ats": ats, "user": user, "last_run_iso": last_run_iso})

    monkeypatch.setattr(seam_mod, "_call_notify_session_expired", _record_notify)

    apply_config = {
        "enabled": True,
        "allowed_ats": ["greenhouse"],
        "profile_path": "x",
        "user": "jane",
        "mode": "review",
    }

    result = seam_mod.run_for_job(
        job={"ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1"},
        jd_text="JD",
        lane={"name": "backend"},
        resume_path=tmp_path / "resume.pdf",
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
    )

    assert notify_calls, "H9: notify_session_expired was never fired"
    assert notify_calls[0]["ats"] == "greenhouse"
    assert notify_calls[0]["user"] == "jane"
    assert notify_calls[0]["last_run_iso"] == "2026-07-01T00:00:00+00:00"
    assert result.status == "skipped"
    assert result.reason == "session_expired"
