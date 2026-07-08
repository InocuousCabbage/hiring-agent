"""
tests/apply/test_dedup_pii_regression.py — extends S5 without editing its
own test_dedup.py file.  S18 adds only the PII-regression check for the
DedupDB write path.
"""

from __future__ import annotations

import pytest


def test_dedup_record_pii_regression(
    tmp_dedup_db, sample_candidate_profile, capture_logs
):
    """
    tmp_dedup_db seeded three canonical rows via .record() during fixture
    setup.  Assert no PII surfaced in structlog events.
    """
    from src.apply.types import ApplyResult

    fresh = ApplyResult(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/testco/jobs/4500000000",
        application_id="app_fresh",
        submitted_at="2026-07-07T13:00:00+00:00",
    )
    tmp_dedup_db.record(
        result=fresh,
        applicant="jane",
        company="Testco",
        role_title="Junior Software Engineer",
        job_url="https://boards.greenhouse.io/testco/jobs/4500000000",
    )
    capture_logs.assert_no_pii(sample_candidate_profile)
