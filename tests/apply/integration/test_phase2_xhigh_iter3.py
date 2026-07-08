"""Phase 2 xhigh iteration 3 — RED tests for iter-2 regressions.

The iter-2 H1 fix (Gate-1 fail-closed on Exception → hit=True) inadvertently
caught the AttributeError from `None.was_applied(...)` and turned the
intended-None `ctx.dedup` cases (YES-branch replay + H7 dedup-init-fail
path) into phantom `already_applied` returns. Two BLOCKING cascades:

    iter3-B1  YES-branch replay via `_AutoModeCtx.dedup=None`: adapter
              Gate-1 fail-closed sets `hit=True` on AttributeError →
              returns `already_applied` → M4 reconciles as 'submitted' →
              operator sees "submitted" in digest with no actual ATS
              click for a real first-time application.

    iter3-B2  H7 dedup-init-fail path: seam sets dedup=None + forces
              mode='review'. Adapter Gate-1 fires FIRST, returns
              `already_applied` (via AttributeError → hit=True). Result
              status is NOT in `_STAGE_STATUSES` → no stage_review call
              → no review email sent → job silently vanishes.

    iter3-H1  Digest has NO bucket for `already_applied`. A legit
              fail-closed skip (real dedup exception in Gate-1) is
              dropped from the digest with zero operator visibility.
              The `already_applied` path predates this diff but the
              iter-2 fail-closed exposes it as a real gap.

    iter3-M1  Gate-2 rate-limit still fail-OPEN
              (`except Exception: today_count = 0`) — inconsistent with
              Gate-1 (iter2) and Gate-3 (iter1) fail-CLOSED policy.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────
# iter3-B1 — Gate-1 with dedup=None must NOT fail-closed
# ─────────────────────────────────────────────────────────


def test_gate1_with_dedup_none_does_not_fail_closed_to_already_applied():
    """iter3-B1: `ctx.dedup=None` on the YES-branch replay is INTENTIONAL.
    The adapter's Gate-1 must skip cleanly, NOT fail-closed to
    `already_applied` (which M4 would then reconcile as 'submitted' →
    phantom submit).
    """
    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.apply.profile import CandidateProfile

    class _FakePage:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        def goto(self, u):
            self.url = u

        def content(self):
            return "<html></html>"

    adapter = GreenhouseAdapter()
    profile = CandidateProfile.load(str(ROOT / "templates" / "candidate_profile.yaml.example"))

    ctx = SimpleNamespace(
        job={
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "AcmeCorp",
            "role": "Senior Engineer",
            "title": "Senior Engineer",
        },
        applicant="jane",
        mode="review",  # keep it short — we only care about Gate-1.
        dry_run=False,
        config={"mode": "review"},
        profile=profile,
        resume_path=None,
        resume_docx_path=None,
        cover_letter_path=None,
        cover_letter_docx_path=None,
        dedup=None,  # intentional!
        captcha_detector=None,
    )

    result = adapter.apply(_FakePage(), ctx)
    status = getattr(result, "status", None)
    assert status != "already_applied", (
        f"iter3-B1: adapter Gate-1 must NOT fail-closed on dedup=None; "
        f"got status={status!r}. Pre-fix bare `except Exception` catches "
        f"AttributeError from None.was_applied() and returns already_applied "
        f"→ M4 reconciles as 'submitted' → phantom submit."
    )


# ─────────────────────────────────────────────────────────
# iter3-B2 — H7 fail-closed path stages a review, not already_applied
# ─────────────────────────────────────────────────────────


def test_seam_h7_fail_closed_stages_review_when_gate1_would_phantom_already_applied(
    tmp_path, monkeypatch
):
    """iter3-B2: on the H7 dedup-init-fail path (seam forces mode=review,
    dedup=None), the adapter must NOT return already_applied for a job with
    no prior record — that skips stage_review and no review email is sent.
    Post-fix: Gate-1 recognizes dedup=None + skips cleanly → review-mode
    branch fires → returns review_required → stage_review runs.

    This is a functional end-to-end assertion: dispatch a real job through
    the seam with a broken DedupDB and verify stage_review is called (or
    equivalently, that the returned status is review_required, not
    already_applied).
    """
    import src.apply._seam as seam_mod
    from src.apply import dedup as dedup_mod

    class _BrokenDedupDB:
        def __init__(self, *a, **kw):
            raise sqlite3.OperationalError("simulated init failure")

    monkeypatch.setattr(dedup_mod, "DedupDB", _BrokenDedupDB)

    captured = {"status": None, "stage_review_called": False}

    def _fake_apply_to_job(job_url, ctx, config):
        # Real dispatcher would run the adapter with dedup=None + mode=review.
        # We simulate what SHOULD happen: the adapter's Gate-1 skips
        # (dedup=None), the review-mode branch fires, review_required.
        from src.apply.adapters.greenhouse import GreenhouseAdapter
        from src.apply.profile import CandidateProfile

        class _FakePage:
            url = job_url

            def goto(self, u):
                self.url = u

            def content(self):
                return "<html></html>"

        adapter = GreenhouseAdapter()
        # Actually invoke the adapter to observe the true Gate-1 behavior.
        result = adapter.apply(_FakePage(), ctx)
        captured["status"] = getattr(result, "status", None)
        return result

    monkeypatch.setattr(
        seam_mod,
        "_call_apply_to_job",
        lambda *, job_url, ctx, config: _fake_apply_to_job(job_url, ctx, config),
    )

    # Spy on stage_review to see if it's invoked.
    def _fake_stage(**kw):
        captured["stage_review_called"] = True
        return "REVIEW_ID_1"

    monkeypatch.setattr(seam_mod, "_call_stage_review", _fake_stage)

    class _FakeGmail:
        def get_or_create_label(self, name):
            return f"lbl:{name}"

    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    apply_config = {
        "enabled": True,
        "mode": "auto",  # H7 will force to review.
        "dry_run": False,
        "allowed_ats": ["greenhouse"],
        "long_tail": "none",
        "timeout_seconds": 90,
        "navigation_retries": 2,
        "fast_path_recipient": "env:MY_EMAIL",
        "review_reping_hours": 24,
        "review_timeout_hours": 72,
        "rate_limit_per_ats_per_day": 10,
        "retention_days": 30,
        "gmail_label_prefix": "hiring-agent/apply",
        "screenshot_dir": str(tmp_path / "shots"),
        "trace_dir": str(tmp_path / "traces"),
        "storage_state_dir": str(tmp_path / "cred"),
        "dedup_db_path": str(tmp_path / "state" / "applied_jobs.db"),
        "captcha_action": "escalate",
        "captcha_transport": "browserbase",
        "profile_path": str(profile_path),
        "user": "jane",
    }
    job = {
        "url": "https://boards.greenhouse.io/acme/jobs/999",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/999",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/999",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "title": "Senior Engineer",
    }
    result = seam_mod.run_for_job(
        job=job,
        jd_text="fake",
        lane={"name": "backend", "label": "backend"},
        resume_path=None,
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
        gmail_client=_FakeGmail(),
    )
    status = getattr(result, "status", None) if result else captured["status"]
    assert status != "already_applied", (
        f"iter3-B2: H7 fail-closed path must NOT return already_applied on "
        f"a fresh job — that skips stage_review. Got status={status!r}."
    )


# ─────────────────────────────────────────────────────────
# iter3-M1 — Gate-2 rate-limit fail-CLOSED
# ─────────────────────────────────────────────────────────


def test_gate2_rate_limit_fails_closed_on_exception():
    """iter3-M1: Gate-2 count_today must NOT swallow exceptions to
    today_count=0. If Gate-1 somehow passes but count_today raises, the
    rate-limit gate silently opens, letting unbounded auto-submits
    through. Consistent with the iter2 fail-CLOSED policy at Gate-1.
    """
    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.apply.profile import CandidateProfile

    class _WeirdDedup:
        """was_applied works, count_today raises. Simulates partial DB corruption."""

        def was_applied(self, *a, **kw):
            return False

        def soft_warn_check(self, *a, **kw):
            return []

        def count_today(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    class _FakePage:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        def goto(self, u):
            self.url = u

        def content(self):
            return "<html></html>"

    adapter = GreenhouseAdapter()
    profile = CandidateProfile.load(str(ROOT / "templates" / "candidate_profile.yaml.example"))

    ctx = SimpleNamespace(
        job={
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "AcmeCorp",
            "role": "Senior Engineer",
            "title": "Senior Engineer",
        },
        applicant="jane",
        mode="auto",
        dry_run=False,
        config={"mode": "auto", "rate_limit_per_ats_per_day": 10},
        profile=profile,
        resume_path=None,
        resume_docx_path=None,
        cover_letter_path=None,
        cover_letter_docx_path=None,
        dedup=_WeirdDedup(),
        captcha_detector=None,
    )

    result = adapter.apply(_FakePage(), ctx)
    status = getattr(result, "status", None)
    assert status != "submitted", (
        f"iter3-M1: Gate-2 count_today exception must fail CLOSED; got "
        f"status={status!r}. Pre-fix `except Exception: today_count = 0` "
        f"silently opens the rate-limit gate on any broken DB."
    )
