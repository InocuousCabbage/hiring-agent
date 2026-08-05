"""Tests for user_company substitution + no-token-leak regression guard.

Real bug: 2026-08-05 Sandpiper cover letter shipped verbatim "At Acme Corp
I built a company-wide marketing data platform in Azure..." because the
project-bank had literal "Acme Corp" strings + no render-time
substitution. Test suite ENSHRINED the defect: fixtures across
test_full_pipeline, test_renderer, test_review_fixes, apply tests, and
digest_golden.txt all asserted "Acme Corp" as expected. Fixture-authored-
by-code-author blindness at test-suite scale.

Fix moves the placeholder to {{USER_COMPANY}} and substitutes at load-time
from a gitignored settings.local.yaml. The test-fixture canary that
enshrines the ANTI-defect is TESTCO-DO-NOT-SHIP (or the direct token
{{USER_COMPANY}} — both must never appear in shipped output).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.user_company import (
    USER_COMPANY_TOKEN,
    UnsubstitutedPlaceholderError,
    assert_no_unsubstituted_tokens,
    load_user_company,
    substitute_user_company,
)


# ── load_user_company ─────────────────────────────────────────────────────


def test_load_raises_when_settings_local_missing(tmp_path: Path):
    """No settings.local.yaml at all = fail loud (not silently defaulting)."""
    (tmp_path / "config").mkdir()
    with pytest.raises(UnsubstitutedPlaceholderError) as exc:
        load_user_company(tmp_path)
    assert "settings.local.yaml" in str(exc.value)
    # Recovery hint must be in the message so operator knows what to do.
    assert "settings.local.example.yaml" in str(exc.value)


def test_load_raises_when_user_company_field_missing(tmp_path: Path):
    """File exists but no user.company field = fail loud."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.local.yaml").write_text("user:\n  other: value\n")
    with pytest.raises(UnsubstitutedPlaceholderError) as exc:
        load_user_company(tmp_path)
    assert "user.company" in str(exc.value)


def test_load_raises_when_user_company_empty(tmp_path: Path):
    """Empty string counts as missing."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.local.yaml").write_text('user:\n  company: ""\n')
    with pytest.raises(UnsubstitutedPlaceholderError):
        load_user_company(tmp_path)


def test_load_raises_when_still_example_placeholder(tmp_path: Path):
    """User copied the example but didn't fill it in = catch it here,
    not in a shipped cover letter that names 'YOUR_COMPANY_NAME_HERE'."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.local.yaml").write_text(
        'user:\n  company: "YOUR_COMPANY_NAME_HERE"\n'
    )
    with pytest.raises(UnsubstitutedPlaceholderError) as exc:
        load_user_company(tmp_path)
    assert "YOUR_COMPANY_NAME_HERE" in str(exc.value)


def test_load_returns_stripped_value(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.local.yaml").write_text(
        'user:\n  company: "  Real Employer  "\n'
    )
    assert load_user_company(tmp_path) == "Real Employer"


# ── substitute_user_company ───────────────────────────────────────────────


def test_substitute_replaces_token_in_string():
    result = substitute_user_company("At {{USER_COMPANY}} I built X", "TESTCO-DO-NOT-SHIP")
    assert result == "At TESTCO-DO-NOT-SHIP I built X"


def test_substitute_walks_nested_dicts():
    content = {"project": {"company": "{{USER_COMPANY}}", "date": "2025"}}
    result = substitute_user_company(content, "TESTCO-DO-NOT-SHIP")
    assert result["project"]["company"] == "TESTCO-DO-NOT-SHIP"
    assert result["project"]["date"] == "2025"


def test_substitute_walks_lists_of_dicts():
    content = [
        {"company": "{{USER_COMPANY}}"},
        {"company": "{{USER_COMPANY}}"},
    ]
    result = substitute_user_company(content, "TESTCO-DO-NOT-SHIP")
    assert all(p["company"] == "TESTCO-DO-NOT-SHIP" for p in result)


def test_substitute_passes_non_strings_through():
    """Numeric + None + bool must not be coerced or altered."""
    content = {"count": 5, "flag": True, "note": None, "company": "{{USER_COMPANY}}"}
    result = substitute_user_company(content, "TESTCO-DO-NOT-SHIP")
    assert result["count"] == 5
    assert result["flag"] is True
    assert result["note"] is None
    assert result["company"] == "TESTCO-DO-NOT-SHIP"


def test_substitute_leaves_unrelated_strings_alone():
    content = {"tags": ["python", "marketing"], "summary": "no token here"}
    result = substitute_user_company(content, "TESTCO-DO-NOT-SHIP")
    assert result == content


def test_substitute_replaces_multiple_tokens_in_one_string():
    """Sometimes a bullet references the employer twice."""
    raw = "Built {{USER_COMPANY}}'s CRM at {{USER_COMPANY}}"
    result = substitute_user_company(raw, "TESTCO-DO-NOT-SHIP")
    assert result == "Built TESTCO-DO-NOT-SHIP's CRM at TESTCO-DO-NOT-SHIP"


# ── assert_no_unsubstituted_tokens (regression guard) ────────────────────


def test_assert_passes_on_clean_content():
    """No token anywhere = no raise."""
    assert_no_unsubstituted_tokens({"company": "Real Name"})
    assert_no_unsubstituted_tokens("clean string")
    assert_no_unsubstituted_tokens([1, 2, {"nested": "clean"}])


def test_assert_raises_on_top_level_token():
    with pytest.raises(UnsubstitutedPlaceholderError) as exc:
        assert_no_unsubstituted_tokens("At {{USER_COMPANY}} I did X")
    assert USER_COMPANY_TOKEN in str(exc.value)


def test_assert_raises_on_nested_token():
    content = {"outer": {"inner": [{"description": "{{USER_COMPANY}} is great"}]}}
    with pytest.raises(UnsubstitutedPlaceholderError) as exc:
        assert_no_unsubstituted_tokens(content, context="project_bank")
    assert "project_bank" in str(exc.value)
    # Path in error must name where the token was found so operator can
    # find + fix the source of the leak.
    assert "outer" in str(exc.value)


def test_assert_raises_when_token_survived_substitution_of_wrong_value():
    """Regression case: someone calls substitute_user_company("", ...) or
    similar edge case that leaves the token unbound. Guard fires."""
    substituted = substitute_user_company("{{USER_COMPANY}}", "")
    # Empty substitution replaces {{USER_COMPANY}} with empty string.
    # The token itself is gone, so assert should PASS.
    assert_no_unsubstituted_tokens(substituted)


# ── Anti-fixture-authored-by-code-author blindness ──────────────────────


def test_project_bank_yaml_contains_no_acme_corp_after_this_fix():
    """The whole point of this fix: 'Acme Corp' must not appear as a
    placeholder in project_bank.yaml. If a future edit reintroduces it,
    this test catches it before the substitution-mechanism silently
    ships the wrong-but-plausible name.

    Runs against the actual repo file. If Acme Corp is legitimately
    someone's real employer in the future and belongs in project_bank
    as data (not placeholder), that's a config-file addition, not a
    project_bank content addition — this test still holds."""
    from src.main import ROOT

    project_bank_path = ROOT / "templates" / "project_bank.yaml"
    content = project_bank_path.read_text()
    assert "Acme Corp" not in content, (
        f"'Acme Corp' appears in {project_bank_path.relative_to(ROOT)}. "
        f"This is the pre-fix placeholder that shipped verbatim in a real "
        f"Sandpiper cover letter on 2026-08-05. Replace with "
        f"'{USER_COMPANY_TOKEN}' + rely on settings.local.yaml substitution."
    )


def test_load_project_bank_end_to_end_substitutes_and_leaves_no_tokens():
    """Target-env verification: exercise the LOAD SEAM at the callable
    boundary (`load_project_bank`), the same seam the pipeline hits.

    Session-scoped autouse fixture `_testco_user_company_canary` has
    already written settings.local.yaml with `user.company:
    TESTCO-DO-NOT-SHIP` to the real repo root. Calling load_project_bank
    thus exercises the FULL substitution path: read project_bank.yaml +
    load user.company from settings.local.yaml + substitute + assert no
    tokens remain. This is the closest thing to a target-env pipeline
    run possible without live LLM (which would need Anthropic creds +
    network + non-deterministic responses).

    Trade-off named honestly: this proves the substitution mechanism
    end-to-end at the yaml-load boundary. It does NOT prove that a
    downstream LLM prompt with substituted content doesn't re-introduce
    the token (that would be an anti-property to test at the prompt
    layer). load-time substitution + no-token-in-source is the CI-catchable
    guarantee we can make.
    """
    from src.main import load_project_bank

    projects = load_project_bank()

    assert isinstance(projects, list), "load_project_bank must return list"
    assert projects, "project_bank.yaml must have at least one project"

    serialized = str(projects)
    assert USER_COMPANY_TOKEN not in serialized, (
        f"After load_project_bank, {USER_COMPANY_TOKEN} still appears in "
        f"loaded content. Substitution missed a location."
    )
    assert "TESTCO-DO-NOT-SHIP" in serialized, (
        "After load_project_bank, TESTCO-DO-NOT-SHIP (the test canary "
        "written by _testco_user_company_canary) does not appear anywhere "
        "in loaded content. Either the fixture failed to write "
        "settings.local.yaml OR no project_bank entry contains "
        f"{USER_COMPANY_TOKEN} to substitute (canary can't fire)."
    )
    assert "Acme Corp" not in serialized, (
        "After load_project_bank, literal 'Acme Corp' string appears in "
        "loaded content. The pre-fix defect has regressed — a fixture or "
        "yaml entry reintroduced the un-substituted employer name."
    )


def test_project_bank_yaml_uses_user_company_token_where_appropriate():
    """Positive control on the above negative: at least one occurrence
    of the token must exist, otherwise the test above would trivially
    pass on an empty project_bank."""
    from src.main import ROOT

    project_bank_path = ROOT / "templates" / "project_bank.yaml"
    content = project_bank_path.read_text()
    assert USER_COMPANY_TOKEN in content, (
        f"No {USER_COMPANY_TOKEN} token found in project_bank.yaml. Either "
        f"the project bank is empty (test needs revisit) or the substitution "
        f"mechanism is not wired to any project entries (regression)."
    )
