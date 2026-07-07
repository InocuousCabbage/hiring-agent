"""
tests/apply/test_profile_pii_regression.py — extends S1 without editing its
own test_profile.py file.  S18 adds only the PII-regression check that
loading the placeholder profile emits no PII into structlog events.
"""

from __future__ import annotations

import pytest


def test_profile_load_pii_regression(
    sample_candidate_profile, capture_logs
):
    """
    Loading the profile (via the sample_candidate_profile fixture, which
    invokes CandidateProfile.load) must NOT leak PII into structlog events.
    S1's loader logs only structural events (profile.loaded,
    profile.validation_failed with key names) — L7 hard boundary.
    """
    # The fixture load already happened during setup; assert on the captured
    # events.  Any first_name / last_name / email substring in a log event
    # value is a regression.
    capture_logs.assert_no_pii(sample_candidate_profile)
