"""Live-suite fixtures for tests/apply/live/**.

Session-scoped opt-in gate for HIRING_AGENT_LIVE_ATS + boards.greenhouse.io
network reachability. Additive to S18's root tests/conftest.py — nothing is
shadowed here (S18 owns sample_candidate_profile, capture_logs, frozen_now,
apply_settings, sample_apply_context, tmp_dedup_db).

Landmine references:
  L9: explicit allowlist for env-var truthy check, never `if os.environ.get(...):`.
Reachability probe uses stdlib urllib.request (NOT `requests`) with a 3s
timeout — a `requests` dependency plus no timeout is a documented BLOCKING
pattern in the spec's code-review pass criteria.
"""
from __future__ import annotations

import os
import urllib.request

import pytest

LIVE_ENV_VAR = "HIRING_AGENT_LIVE_ATS"
# L9: explicit allowlist — never a truthy check. "0" and "false" MUST NOT opt in.
LIVE_ENV_ALLOWLIST = ("1", "true", "yes")
LIVE_HEAD_URL = "https://boards.greenhouse.io"
LIVE_HEAD_TIMEOUT_S = 3.0


@pytest.fixture(scope="session")
def require_live_env() -> None:
    """Gate every live_ats test on env-var opt-in + network reachability.

    Skips (never fails) when either:
      (a) $HIRING_AGENT_LIVE_ATS is unset or not in LIVE_ENV_ALLOWLIST, or
      (b) the boards.greenhouse.io HEAD probe fails within 3s.

    Skip reason is descriptive (acceptance #9).
    """
    value = os.environ.get(LIVE_ENV_VAR)
    if value not in LIVE_ENV_ALLOWLIST:
        pytest.skip(
            f"{LIVE_ENV_VAR} not set (or not in {LIVE_ENV_ALLOWLIST!r}); "
            "live suite opt-in only — never runs in CI."
        )
    try:
        # stdlib urllib.request — NOT `requests` (spec BLOCKING criterion).
        req = urllib.request.Request(LIVE_HEAD_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=LIVE_HEAD_TIMEOUT_S):
            pass
    except Exception as e:  # broad by design — any probe failure = skip, not fail
        pytest.skip(f"boards.greenhouse.io unreachable (HEAD probe failed): {e!r}")
