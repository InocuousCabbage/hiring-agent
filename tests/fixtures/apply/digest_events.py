"""Factory functions for the five ApplyEvent kinds S14 renders.

Spec §Interfaces + §TDD scaffolding lock the row shape used by each renderer.
These factories keep tests concise and consistent — each returns an
``ApplyEvent`` populated with sensible defaults that individual tests can
override via kwargs.
"""

from __future__ import annotations

# ``sys.path`` insertion is handled by test_digest.py's own bootstrap; when
# imported directly by the fixture consumers, we rely on the same shim.
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from apply.types import ApplyEvent  # noqa: E402  (path shim above)


def make_submitted_event(**overrides) -> ApplyEvent:
    row = {
        "ats": "greenhouse",
        "application_id": "12345",
        "submitted_at": "2026-07-07T15:04:00+00:00",
        # PII fields that MUST NOT bleed into the digest body (L7).
        "candidate_email": "secret@example.com",
        "candidate_first_name": "Ben",
        "candidate_last_name": "Joslin",
        "candidate_phone": "+1-555-0100",
        "linkedin_url": "https://linkedin.com/in/secret",
    }
    row.update(overrides.pop("row_overrides", {}))
    row.update({k: v for k, v in overrides.items() if k not in {"kind"}})
    return ApplyEvent(kind="submitted", row=row)


def make_review_required_event(**overrides) -> ApplyEvent:
    row = {
        "ats": "greenhouse",
        "review_id": "rev-abc",
        "gmail_thread_id": "thread-xyz",
        "screenshot_path": None,
        "candidate_email": "secret@example.com",
        "candidate_first_name": "Ben",
        "candidate_last_name": "Joslin",
        "candidate_phone": "+1-555-0100",
    }
    row.update(overrides.pop("row_overrides", {}))
    row.update({k: v for k, v in overrides.items() if k not in {"kind"}})
    return ApplyEvent(kind="review_required", row=row)


def make_auto_declined_event(**overrides) -> ApplyEvent:
    row = {
        "ats": "greenhouse",
        "review_id": "rev-old",
        "company": "Acme",
        "title": "Senior Engineer",
        "candidate_email": "secret@example.com",
    }
    row.update(overrides.pop("row_overrides", {}))
    row.update({k: v for k, v in overrides.items() if k not in {"kind"}})
    return ApplyEvent(kind="auto_declined", row=row)


def make_soft_dup_event(**overrides) -> ApplyEvent:
    row = {
        "ats": "greenhouse",
        "review_id": "rev-dup",
        "company": "Acme",
        "similar_role": "Senior Engineer",
        "candidate_email": "secret@example.com",
    }
    row.update(overrides.pop("row_overrides", {}))
    row.update({k: v for k, v in overrides.items() if k not in {"kind"}})
    return ApplyEvent(kind="soft_dup", row=row)


def make_bootstrap_needed_event(**overrides) -> ApplyEvent:
    row = {
        "ats": "greenhouse",
        "reason": "session_expired",
        "candidate_email": "secret@example.com",
    }
    row.update(overrides.pop("row_overrides", {}))
    row.update({k: v for k, v in overrides.items() if k not in {"kind"}})
    return ApplyEvent(kind="bootstrap_needed", row=row)
