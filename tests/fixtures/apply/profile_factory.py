"""Shared test-fixture factory for the S1 CandidateProfile.

S2 originally constructed a minimal 2-field CandidateProfile inline in its
test files (`name` + `contact`). S1's real dataclass requires 7 fields
(name, contact, address, work_authorization, eeo, compensation, references)
because address/eeo/compensation/references have no defaults.

S17 responsibility #4 reconciles this by giving S2/S17/S18-style tests a
single call site that loads the canonical `templates/candidate_profile.yaml.example`
via `CandidateProfile.load()` — the same path the production config-gate
(S3) validates. Callers can override individual sub-fields with kwargs
for test-specific shape tweaks.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.apply.profile import CandidateProfile

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "templates"
    / "candidate_profile.yaml.example"
)


def load_example_profile() -> CandidateProfile:
    """Return a `CandidateProfile` from the checked-in template.

    Zero mutation. Idempotent — every call returns a fresh dataclass since
    S1's dataclass is frozen.
    """
    return CandidateProfile.load(_TEMPLATE_PATH)


def make_profile(**overrides: Any) -> CandidateProfile:
    """Return the template `CandidateProfile` with top-level fields swapped in.

    Example:
        profile = make_profile(name=Name(first="Alice", last="Doe", full="Alice Doe"))
    """
    base = load_example_profile()
    if not overrides:
        return base
    return replace(base, **overrides)
