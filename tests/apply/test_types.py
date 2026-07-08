"""RED tests for src.apply.types — S2 shard.

These tests freeze the cross-shard shapes defined in master-plan §4.1, §4.3.
Any downstream shard that widens/narrows these MUST amend S2's spec first.
"""

from __future__ import annotations

import dataclasses
import typing
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ApplyResult (§4.1)
# ---------------------------------------------------------------------------

def test_apply_result_is_frozen():
    """ApplyResult must be @dataclass(frozen=True) — mutations raise."""
    from src.apply.types import ApplyResult

    result = ApplyResult(status="submitted")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


def test_status_literal_has_exactly_8_values():
    """Status Literal must contain exactly the 8 values from master-plan §4.1."""
    from src.apply.types import Status

    args = typing.get_args(Status)
    assert set(args) == {
        "submitted",
        "review_required",
        "skipped",
        "failed",
        "already_applied",
        "soft_dup_warn",
        "captcha_escalated",
        "auto_declined",
    }
    assert len(args) == 8


def test_apply_result_defaults_all_optional_fields_to_none():
    """Only `status` is required; every other ApplyResult field defaults to None."""
    from src.apply.types import ApplyResult

    r = ApplyResult(status="submitted")
    assert r.ats is None
    assert r.apply_url is None
    assert r.application_id is None
    assert r.confirmation_screenshot is None
    assert r.reason is None
    assert r.human_review_url is None
    assert r.submitted_at is None
    assert r.trace_path is None
    assert r.review_id is None


# ---------------------------------------------------------------------------
# ApplyContext (§4.3, embedding CandidateProfile from S1)
# ---------------------------------------------------------------------------

def test_apply_context_carries_candidate_profile():
    """ApplyContext.profile must be a CandidateProfile with reachable .contact.email."""
    from src.apply.types import ApplyContext
    from tests.fixtures.apply.profile_factory import load_example_profile

    profile = load_example_profile()
    ctx = ApplyContext(
        profile=profile,
        job={"url": "https://boards.greenhouse.io/example/jobs/12345"},
        resume_path=Path("/tmp/resume.pdf"),
        cover_letter_path=None,
        config={"apply": {"mode": "review"}},
        applicant="jane",
        dry_run=True,
        mode="review",
    )
    # Fixture template email is jane@example.com (see
    # templates/candidate_profile.yaml.example).
    assert ctx.profile.contact.email == "jane@example.com"


def test_apply_context_is_frozen():
    """ApplyContext must be @dataclass(frozen=True)."""
    from src.apply.types import ApplyContext
    from tests.fixtures.apply.profile_factory import load_example_profile

    ctx = ApplyContext(
        profile=load_example_profile(),
        job={},
        resume_path=Path("/tmp/r.pdf"),
        cover_letter_path=None,
        config={},
        applicant="j",
        dry_run=False,
        mode="review",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.applicant = "someone_else"  # type: ignore[misc]


def test_apply_context_accepts_docx_only_lane():
    """AUDIT: Contract widened for dual-output renderer's docx-only lane.

    When render_resume() returns (None, docx_path), the seam must construct
    an ApplyContext with resume_path=None + resume_docx_path=Path(...). The
    dataclass MUST accept this shape.
    """
    from src.apply.types import ApplyContext
    from tests.fixtures.apply.profile_factory import load_example_profile

    ctx = ApplyContext(
        profile=load_example_profile(),
        job={"url": "https://boards.greenhouse.io/example/jobs/1"},
        resume_path=None,
        cover_letter_path=None,
        config={"apply": {}},
        applicant="jane",
        dry_run=True,
        mode="review",
        resume_docx_path=Path("/tmp/resume.docx"),
        cover_letter_docx_path=Path("/tmp/cover.docx"),
    )
    assert ctx.resume_path is None
    assert ctx.resume_docx_path.name == "resume.docx"
    assert ctx.cover_letter_docx_path.name == "cover.docx"


# ---------------------------------------------------------------------------
# FieldFill  (S17 reconciliation: canonical location is
#             src.apply.adapters._labels per S8 spec §File-ownership.
#             The `apply.__init__` re-exports it so `from src.apply import
#             FieldFill` still works — that surface is the frozen contract.)
# ---------------------------------------------------------------------------

def test_field_fill_strategy_literal_has_expected_values():
    """FieldFill.strategy Literal must cover the 4 driver-execution strategies."""
    pytest.importorskip("src.apply.adapters._labels")
    from src.apply import FieldFill  # re-exported from _labels

    hints = typing.get_type_hints(FieldFill)
    strategy_args = set(typing.get_args(hints["strategy"]))
    assert strategy_args == {
        "fill",
        "select_option_by_label",
        "check",
        "upload",
    }


def test_field_fill_is_frozen():
    """FieldFill must be @dataclass(frozen=True)."""
    pytest.importorskip("src.apply.adapters._labels")
    from src.apply import FieldFill

    f = FieldFill(
        selector="#email",
        strategy="fill",
        value="jane@example.com",
        label="Email",
        required=True,
        source="label_scan",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.value = "hacker@example.com"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SessionContext (§4.3)
# ---------------------------------------------------------------------------

def test_session_context_transport_literal_has_two_values():
    """SessionContext.transport Literal must be exactly {'local','browserbase'}."""
    from src.apply.types import SessionContext

    hints = typing.get_type_hints(SessionContext)
    assert set(typing.get_args(hints["transport"])) == {"local", "browserbase"}


def test_session_context_is_frozen():
    """SessionContext must be frozen."""
    from src.apply.types import SessionContext

    sc = SessionContext(
        transport="local",
        replay_url=None,
        trace_path=None,
        proxies_enabled=False,
        solve_captchas=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        sc.transport = "browserbase"  # type: ignore[misc]
