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

# hiring.cafe /job #__NEXT_DATA__ fixtures (JD-extraction modernization, HALF 1).
# Captured 2026-09-09 from a real public posting; PII scrubbed per conftest
# invariants 5-7. hiring.cafe's page JSON schema is external and unversioned.
HIRINGCAFE_FIXTURES: Path = REPO_ROOT / "tests" / "fixtures" / "hiringcafe"
HIRINGCAFE_JOB_NEXT_DATA_JSON: Path = HIRINGCAFE_FIXTURES / "job_next_data.json"
HIRINGCAFE_JOB_PAGE_HTML: Path = HIRINGCAFE_FIXTURES / "job_page.html"
