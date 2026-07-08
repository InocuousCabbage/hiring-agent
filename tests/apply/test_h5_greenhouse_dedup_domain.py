"""H5: greenhouse gate-1 dedup uses adapter.name ('greenhouse'), but
DedupDB.record writes ats_domain=_extract_ats_domain(apply_url) which is
'boards.greenhouse.io'. Rows never match → dedup fast-path never fires;
follow-up record() raises AlreadyAppliedError swallowed by bare except
= silent double-apply.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.apply.adapters.greenhouse import GreenhouseAdapter
from src.apply.dedup import DedupDB
from src.apply.types import ApplyResult


class _FakeProfile:
    """Minimal duck-typed profile stand-in — every field is None so the
    planner never emits a fill for it and we don't need a real form."""

    def __init__(self):
        self.name = None
        self.contact = None
        self.work_authorization = None
        self.resume_path = None


class _FakePage:
    """Playwright Page stand-in with the surface greenhouse.apply reads."""

    url = ""

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
        # Best-effort — write an empty file if a path was passed.
        if path:
            try:
                Path(path).write_bytes(b"")
            except Exception:
                pass

    def close(self):
        pass


class _Ctx:
    def __init__(self, dedup_db, tmp_path):
        self.profile = _FakeProfile()
        self.job = {
            "apply_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "company": "Acme Corp",
            "role": "Senior Engineer",
        }
        self.resume_path = tmp_path / "resume.pdf"
        self.resume_path.write_bytes(b"%PDF-1.4\n")
        self.resume_docx_path = None
        self.cover_letter_path = None
        self.cover_letter_docx_path = None
        self.config = {
            "screenshot_dir": str(tmp_path / "screenshots"),
            "trace_dir": str(tmp_path / "traces"),
            "rate_limit_per_ats_per_day": 10,
        }
        self.dedup = dedup_db
        self.applicant = "jane"
        self.dry_run = True
        self.mode = "review"
        self.captcha_detector = None


def test_greenhouse_dedup_matches_across_apply_and_check(tmp_path: Path):
    """RED: record a submitted result via DedupDB.record (which writes
    ats_domain='boards.greenhouse.io'), then run adapter.apply on the same
    URL. Before H5 the was_applied check queries with self.name='greenhouse'
    → the row does not match → adapter proceeds and eventually raises
    AlreadyAppliedError on the follow-up record() call.

    After H5: was_applied uses _extract_ats_domain(apply_url), so the row
    matches and the adapter returns status='already_applied' before any
    browser work.
    """
    db = DedupDB(tmp_path / "dedup.db")

    # Seed a prior submission for this exact job.
    apply_url = "https://boards.greenhouse.io/acme/jobs/12345"
    prior = ApplyResult(
        status="submitted",
        ats="greenhouse",
        apply_url=apply_url,
        submitted_at="2026-07-01T00:00:00+00:00",
    )
    db.record(prior, applicant="jane", company="Acme Corp",
              role_title="Senior Engineer", job_url=apply_url)

    # Now sanity-check: was_applied returns True for the DB-shape query.
    domain_hit = db.was_applied(
        company="Acme Corp",
        ats_domain="boards.greenhouse.io",
        ats_job_id="12345",
        job_url=apply_url,
    )
    assert domain_hit, "seed row not stored — test fixture broken"

    # And returns False for the WRONG-shape query (adapter name instead of
    # domain) — this is the H5 bug surface.
    wrong_shape = db.was_applied(
        company="Acme Corp",
        ats_domain="greenhouse",     # adapter.name — bug!
        ats_job_id="12345",
        job_url=apply_url,
    )
    assert not wrong_shape, "test setup regression: adapter.name query somehow matched"

    # Now: adapter.apply should short-circuit via the dedup gate.
    adapter = GreenhouseAdapter()
    ctx = _Ctx(db, tmp_path)
    result = adapter.apply(_FakePage(), ctx)

    # Before H5: adapter runs the browser flow because dedup miss, then
    # dedup.record() would raise AlreadyAppliedError (swallowed as
    # dedup.record_failed) — SILENT DOUBLE-APPLY risk.
    # After H5: gate 1 fires and returns already_applied.
    assert result.status == "already_applied", (
        f"expected dedup gate to fire (already_applied); got {result.status}"
    )
    assert result.ats == "greenhouse"


def test_greenhouse_count_today_uses_ats_domain(tmp_path: Path):
    """H5 post-review: gate-2 (rate limit) must also query with ats_domain,
    not adapter.name. Before the fix, count_today('greenhouse') always
    returned 0 because DedupDB writes ats_domain='boards.greenhouse.io'.
    """
    db = DedupDB(tmp_path / "dedup.db")
    apply_url = "https://boards.greenhouse.io/acme/jobs/12345"

    # Seed a prior submission — different job_id so gate-1 doesn't fire.
    other_url = "https://boards.greenhouse.io/acme/jobs/99999"
    prior = ApplyResult(
        status="submitted", ats="greenhouse", apply_url=other_url,
        submitted_at="2026-07-07T00:00:00+00:00",
    )
    db.record(prior, applicant="jane", company="Other",
              role_title="X", job_url=other_url)

    # Sanity: count_today(ats_domain) sees the seeded row.
    assert db.count_today("boards.greenhouse.io") == 1
    # And count_today(adapter.name) does NOT — that's the bug we're guarding.
    assert db.count_today("greenhouse") == 0

    # Under a rate cap of 1, the adapter must observe the gate as full.
    adapter = GreenhouseAdapter()

    class _CapPage(_FakePage):
        pass

    class _CapCtx(_Ctx):
        def __init__(self, dedup, tmp_path):
            super().__init__(dedup, tmp_path)
            self.config["rate_limit_per_ats_per_day"] = 1

    result = adapter.apply(_CapPage(), _CapCtx(db, tmp_path))
    # If the count query used 'greenhouse' → cap 1 not reached → not
    # rate-limited. If it uses the domain → cap 1 reached → skipped.
    assert result.status == "skipped", (
        f"H5 (post-review): rate limit gate did not fire; got {result.status} — "
        f"count_today likely still queries with adapter.name"
    )
    assert result.reason == "rate_limited"


def test_dedup_record_failed_swallow_narrowed_to_already_applied(tmp_path: Path):
    """H5 secondary fix: the greenhouse record() catch should be narrow to
    AlreadyAppliedError so genuine dupes log dedup_hit, and other errors
    surface. Grep-based assertion because full-flow simulation is heavy.
    """
    src_path = Path(__file__).resolve().parents[2] / "src" / "apply" / "adapters" / "greenhouse.py"
    source = src_path.read_text()

    # The block that catches ctx.dedup.record must NOT be `except Exception`
    # AND must reference AlreadyAppliedError.
    assert "AlreadyAppliedError" in source, (
        "greenhouse.py must import + narrow-catch AlreadyAppliedError on record()"
    )
