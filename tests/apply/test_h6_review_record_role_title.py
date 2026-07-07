"""H6: review.execute_confirmed_submit's dedup_db.record call omits the
required role_title kwarg. TypeError fires on first successful YES-branch
resubmit → tick aborts.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.apply.dedup import AlreadyAppliedError
from src.apply.review import Decision, execute_confirmed_submit


def test_review_yes_branch_records_with_role_title():
    """RED: on a submitted result, dedup_db.record must receive role_title.

    Before the fix, the record call passes (result, applicant=, company=,
    job_url=) — missing role_title. Since DedupDB.record has role_title as
    a positional-or-kw required arg, the call raises TypeError.
    """
    decision = Decision(
        review_id="0195c5a0-1234-7abc-8def-999999999999",
        status="submitted",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        company="Acme Corp",
        role_title="Senior Engineer",
        applicant="jane",
        thread_id="THREAD_777",
    )

    # Fake session ctx that yields a page.
    from contextlib import contextmanager

    class _Page:
        url = ""
        def goto(self, url): self.url = url

    @contextmanager
    def _session_ctx(*, storage_state_path, headless):
        yield (_Page(), None)

    # Fake adapter returns a submitted result.
    class _FakeResult:
        status = "submitted"
        ats = "greenhouse"
        apply_url = "https://boards.greenhouse.io/acme/jobs/1"
        application_id = None
        confirmation_screenshot = None
        reason = None
        human_review_url = None
        submitted_at = "2026-07-07T00:00:00+00:00"
        trace_path = None
        review_id = None

    adapter = MagicMock()
    adapter.apply.return_value = _FakeResult()

    # Fake dedup DB that records the actual call.
    class _FakeDedupDB:
        def __init__(self):
            self.record_calls = []
        def was_applied(self, **kwargs):
            return False
        def record(self, result, **kwargs):
            # Snapshot the call — the H6 fix must include role_title.
            self.record_calls.append(kwargs)

    dedup_db = _FakeDedupDB()

    result = execute_confirmed_submit(
        decision,
        adapter,
        config={"apply": {"dry_run": False}},
        session_ctx=_session_ctx,
        load_state_fn=lambda ats, applicant: None,
        dedup_db=dedup_db,
    )

    assert result.status == "submitted"
    assert len(dedup_db.record_calls) == 1, "dedup.record was not called on the submitted result"
    call = dedup_db.record_calls[0]
    assert "role_title" in call, f"H6: record() call missing role_title kwarg — got kwargs {list(call)}"
    assert call["role_title"] == "Senior Engineer"
    # Sanity: the other required kwargs are still present.
    assert call["applicant"] == "jane"
    assert call["company"] == "Acme Corp"
    assert call["job_url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_review_yes_branch_catches_already_applied_on_replay():
    """H6 post-review: DedupDB.record catches sqlite3.IntegrityError and
    re-raises AlreadyAppliedError. execute_confirmed_submit must catch that,
    not just sqlite3.IntegrityError — otherwise an idempotent replay loses
    the whole poll batch."""
    decision = Decision(
        review_id="0195c5a0-1234-7abc-8def-999999999999",
        status="submitted",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        company="Acme Corp",
        role_title="Senior Engineer",
        applicant="jane",
        thread_id="THREAD_777",
    )

    from contextlib import contextmanager

    class _Page:
        url = ""
        def goto(self, url): self.url = url

    @contextmanager
    def _session_ctx(*, storage_state_path, headless):
        yield (_Page(), None)

    class _FakeResult:
        status = "submitted"
        ats = "greenhouse"
        apply_url = "https://boards.greenhouse.io/acme/jobs/1"
        application_id = None
        confirmation_screenshot = None
        reason = None
        human_review_url = None
        submitted_at = "2026-07-07T00:00:00+00:00"
        trace_path = None
        review_id = None

    adapter = MagicMock()
    adapter.apply.return_value = _FakeResult()

    class _FakeDedupDB:
        def was_applied(self, **kwargs):
            return False
        def record(self, result, **kwargs):
            # Simulate the replay path — record raises AlreadyAppliedError.
            raise AlreadyAppliedError("already applied: replay")

    result = execute_confirmed_submit(
        decision, adapter, config={"apply": {"dry_run": False}},
        session_ctx=_session_ctx,
        load_state_fn=lambda ats, applicant: None,
        dedup_db=_FakeDedupDB(),
    )

    # Must return already_applied (not raise, not fall through as 'submitted').
    assert result.status == "already_applied"
