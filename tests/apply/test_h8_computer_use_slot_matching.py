"""H8: computer_use slot-matching uses `"cover" in name_attr` and
`"resume" in name_attr or "cv" in name_attr`. Substring match hits
false positives:
- `portfolio_cv_samples` → matches 'cv' → resume routed into portfolio slot.
- `coverage_letter` → matches 'cover' → cover routed into unrelated field.

Fix: use word-boundary regex to only match discrete tokens.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.apply.adapters.computer_use import ComputerUseAdapter


class _FakeInput:
    def __init__(self, name_attr):
        self._name = name_attr

    def get_attribute(self, key):
        if key == "name":
            return self._name
        return None


class _RecordingPage:
    """Page fake that records every set_input_files call so we can prove
    which inputs were touched by the short-circuit."""

    def __init__(self, inputs):
        self._inputs = inputs
        self.set_input_files_calls: list[tuple[str, str]] = []

    def query_selector_all(self, selector):
        return self._inputs

    def set_input_files(self, selector, path):
        self.set_input_files_calls.append((selector, path))

    @property
    def url(self):
        return "https://example.com/apply"


class _Ctx:
    def __init__(self, tmp_path):
        self.resume_path = tmp_path / "resume.pdf"
        self.resume_path.write_bytes(b"%PDF-1.4\n")
        self.resume_docx_path = None
        self.cover_letter_path = tmp_path / "cover.pdf"
        self.cover_letter_path.write_bytes(b"%PDF-1.4\n")
        self.cover_letter_docx_path = None
        self.config = {}
        self.mode = "review"
        self.dry_run = True


def test_computer_use_slot_matching_rejects_portfolio_cv_samples(tmp_path: Path):
    """RED: an input named `portfolio_cv_samples` MUST NOT receive the resume.

    Before H8: 'cv' substring match → resume.pdf stapled into the portfolio
    file input. That's the exact collision the surrounding comment says to
    prevent.
    """
    inputs = [_FakeInput("portfolio_cv_samples")]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    # No set_input_files call should reference the portfolio input.
    portfolio_calls = [c for c in page.set_input_files_calls if "portfolio_cv_samples" in c[0]]
    assert not portfolio_calls, (
        f"H8: portfolio_cv_samples was stapled with a resume: {portfolio_calls}"
    )


def test_computer_use_slot_matching_rejects_coverage_letter(tmp_path: Path):
    """RED: an input named `coverage_letter` MUST NOT receive the cover letter.

    Before H8: 'cover' substring match hits coverage_letter → cover letter
    stapled into an unrelated field.
    """
    inputs = [_FakeInput("coverage_letter")]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    coverage_calls = [c for c in page.set_input_files_calls if "coverage_letter" in c[0]]
    assert not coverage_calls, (
        f"H8: coverage_letter was stapled with cover letter: {coverage_calls}"
    )


def test_computer_use_slot_matching_still_matches_resume(tmp_path: Path):
    """The fix must NOT break the happy path: a real 'resume' input receives
    the resume file."""
    inputs = [_FakeInput("resume")]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    resume_calls = [c for c in page.set_input_files_calls if "resume" in c[0]]
    assert resume_calls, "regression: 'resume' input no longer receives the resume"


def test_computer_use_slot_matching_still_matches_cover_letter(tmp_path: Path):
    inputs = [_FakeInput("cover_letter")]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    calls = [c for c in page.set_input_files_calls if "cover_letter" in c[0]]
    assert calls, "regression: 'cover_letter' input no longer receives cover letter"


def test_computer_use_slot_matching_still_matches_cv_token(tmp_path: Path):
    """`cv` as a distinct token still matches resume slot (whole-word)."""
    inputs = [_FakeInput("cv")]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    calls = [c for c in page.set_input_files_calls if 'name="cv"' in c[0]]
    assert calls, "regression: 'cv' token input no longer receives the resume"


# H8 post-review: additional coverage for the token-based slot matcher.


@pytest.mark.parametrize("name_attr", [
    "resume_upload",
    "cv_upload",
    "resume_pdf",
    "resume-file",
    "my_resume",
    "curriculumVitae",
    "curriculum_vitae",
])
def test_h8_common_ats_resume_field_names_matched(tmp_path: Path, name_attr: str):
    """Common ATS field names with underscore or dash separators must still
    route the resume file. The old `\\bresume\\b` regex missed these because
    Python's `\\b` treats `_` as a word char.
    """
    inputs = [_FakeInput(name_attr)]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    resume_matches = [c for c in page.set_input_files_calls if str(c[1]).endswith("resume.pdf")]
    assert resume_matches, f"H8 (post-review): {name_attr!r} did not route the resume file"


@pytest.mark.parametrize("name_attr", [
    "cover_letter_upload",
    "cover-letter-upload",
    "coverLetter",
    "coverletter",
])
def test_h8_common_ats_cover_letter_field_names_matched(tmp_path: Path, name_attr: str):
    inputs = [_FakeInput(name_attr)]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    cover_matches = [c for c in page.set_input_files_calls if str(c[1]).endswith("cover.pdf")]
    assert cover_matches, f"H8 (post-review): {name_attr!r} did not route the cover letter"


@pytest.mark.parametrize("name_attr", [
    "portfolio-cv-samples",
    "portfolio_cv_samples",
    "writing_samples",
    "references",
    "reference_letter",
    "coverage_letter",
])
def test_h8_denylisted_slots_never_receive_resume_or_cover(tmp_path: Path, name_attr: str):
    """These slots must be SKIPPED — no set_input_files call at all."""
    inputs = [_FakeInput(name_attr)]
    page = _RecordingPage(inputs)
    ctx = _Ctx(tmp_path)

    adapter = ComputerUseAdapter()
    adapter._file_upload_short_circuit(page, ctx)

    assert not page.set_input_files_calls, (
        f"H8 (post-review): {name_attr!r} was stapled: {page.set_input_files_calls}"
    )
