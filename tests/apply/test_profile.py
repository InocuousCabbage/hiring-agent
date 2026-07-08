"""RED tests for Shard S1: CandidateProfile loader.

Spec: .agent/one-big-feature/auto-apply-2026-07-06/03-specs/01-s1-profile-loader.md
All tests are written FIRST and must fail before implementation lands.
Fixtures live at tests/fixtures/apply/*.yaml; only placeholder PII allowed.
"""
from __future__ import annotations

import dataclasses
import logging
import textwrap
from pathlib import Path

import pytest

from src.apply.profile import CandidateProfile, ProfileValidationError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "apply"
VALID = FIXTURES / "profile_valid.yaml"
MISSING_EMAIL = FIXTURES / "profile_missing_email.yaml"


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_valid_yaml_returns_frozen_dataclass() -> None:
    profile = CandidateProfile.load(VALID)
    assert profile.name.first == "Jane"
    assert profile.contact.email == "jane@example.com"
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.name = None  # type: ignore[misc]


def test_load_missing_email_raises_validation_error() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        CandidateProfile.load(MISSING_EMAIL)
    assert "contact.email" in str(excinfo.value)


def test_load_unknown_top_level_key_raises(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: jane@example.com
          phone: "+1-555-0100"
        foo: bar
        """,
    )
    with pytest.raises(ProfileValidationError) as excinfo:
        CandidateProfile.load(path)
    msg = str(excinfo.value)
    assert "unknown key" in msg
    assert "foo" in msg


def test_full_name_synthesized_when_omitted(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: jane@example.com
          phone: "+1-555-0100"
        """,
    )
    profile = CandidateProfile.load(path)
    assert profile.name.full == "Jane Doe"


def test_legacy_contact_string_matches_renderer_format(tmp_path: Path) -> None:
    """Byte-identical drop-in for src/pdf_gen/renderer.py:232-233 (per spec §4.4 / AC #8).

    Covers both branches: phone present and phone null.
    """
    profile = CandidateProfile.load(VALID)
    assert profile.legacy_contact_string() == "Jane Doe | jane@example.com | +1-555-0100"

    null_phone_path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: jane@example.com
          phone: null
        """,
    )
    null_phone_profile = CandidateProfile.load(null_phone_path)
    assert null_phone_profile.legacy_contact_string() == "Jane Doe | jane@example.com | "


def test_eeo_all_null_by_default(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: jane@example.com
          phone: "+1-555-0100"
        """,
    )
    profile = CandidateProfile.load(path)
    assert profile.eeo.gender is None
    assert profile.eeo.race_ethnicity is None
    assert profile.eeo.veteran_status is None
    assert profile.eeo.disability_status is None
    assert profile.eeo.pronouns is None


def test_bad_email_format_raises(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: "not-an-email"
        """,
    )
    with pytest.raises(ProfileValidationError) as excinfo:
        CandidateProfile.load(path)
    assert "contact.email" in str(excinfo.value)


def test_short_phone_raises(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        name:
          first: Jane
          last: Doe
        contact:
          email: jane@example.com
          phone: "12345"
        """,
    )
    with pytest.raises(ProfileValidationError) as excinfo:
        CandidateProfile.load(path)
    assert "contact.phone" in str(excinfo.value)


def test_load_never_logs_field_values(caplog: pytest.LogCaptureFixture) -> None:
    """L7 landmine guard: loader may emit structural events but never field values."""
    with caplog.at_level(logging.DEBUG):
        CandidateProfile.load(VALID)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    # Also capture any structlog-style key=value pairs on the record itself.
    for record in caplog.records:
        combined += "\n" + " ".join(f"{k}={v!r}" for k, v in record.__dict__.items())
    forbidden = ["Jane", "Doe", "jane@example.com", "555-0100", "123 Main St", "Portland"]
    leaks = [needle for needle in forbidden if needle in combined]
    assert not leaks, f"Loader logged PII: {leaks}"
