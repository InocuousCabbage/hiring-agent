"""
tests/apply/_paths.py — repo-relative fixture-path constants shared across
S18's test modules.

Kept out of tests/conftest.py so tests can `from tests.apply._paths import
GREENHOUSE_FORM_HTML` without depending on `tests/` being importable as a
package (some pytest importmodes make top-level `from tests.conftest`
brittle).  conftest.py re-imports these constants for its own use.
"""

from __future__ import annotations

from pathlib import Path

# tests/apply/_paths.py -> tests/apply -> tests -> <repo root>
REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
FIXTURES: Path = REPO_ROOT / "tests" / "fixtures" / "apply"

GREENHOUSE_FORM_HTML: Path = FIXTURES / "greenhouse_form.html"
GREENHOUSE_CONFIRMATION_HTML: Path = FIXTURES / "greenhouse_confirmation.html"
GREENHOUSE_BOARDS_API_JSON: Path = FIXTURES / "greenhouse_boards_api.json"
PROFILE_VALID_YAML: Path = FIXTURES / "profile_valid.yaml"
