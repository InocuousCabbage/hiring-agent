"""H15: greenhouse's `upload_path = resume_path or resume_docx_path` is
truthy when resume_path is Path("") — because Path("") equals PosixPath("."),
which is truthy → set_input_files gets "." → Playwright error.

Fix: verify existence AND non-empty stem before selecting; if both fail,
fall back to failed(reason=no_resume_available).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.apply.adapters.greenhouse import GreenhouseAdapter, RESUME_SENTINEL
from src.apply.types import ApplyResult


class _FakePage:
    """Playwright fake that returns the sentinel FieldFill on scan and
    records set_input_files calls."""

    def __init__(self):
        self.url = ""
        self.set_input_files_calls = []

    def goto(self, url):
        self.url = url

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        loc = MagicMock()
        loc.count.return_value = 1  # pretend the selector is present
        loc.first = loc
        return loc

    def select_option(self, selector, label=None):
        pass

    def check(self, selector):
        pass

    def fill(self, selector, value):
        pass

    def set_input_files(self, selector, path):
        self.set_input_files_calls.append((selector, path))

    def screenshot(self, path=None):
        if path:
            try:
                Path(path).write_bytes(b"")
            except Exception:
                pass

    def close(self):
        pass


class _FakeProfile:
    def __init__(self):
        self.name = None
        self.contact = None
        self.work_authorization = None


class _Ctx:
    def __init__(self, tmp_path, resume_path, resume_docx_path):
        self.profile = _FakeProfile()
        self.job = {
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "role": "Engineer",
        }
        self.resume_path = resume_path
        self.resume_docx_path = resume_docx_path
        self.cover_letter_path = None
        self.cover_letter_docx_path = None
        self.config = {
            "screenshot_dir": str(tmp_path / "screenshots"),
            "trace_dir": str(tmp_path / "traces"),
        }
        self.dedup = _FakeDedup()
        self.applicant = "jane"
        self.dry_run = True
        self.mode = "review"
        self.captcha_detector = None


class _FakeDedup:
    def was_applied(self, *args, **kwargs):
        return False

    def count_today(self, *args, **kwargs):
        return 0

    def soft_warn_check(self, *args, **kwargs):
        return []


def _stub_plan(monkeypatch, has_upload=True):
    """Patch the module-level plan_form_fill to return a single RESUME_SENTINEL
    FieldFill so we exercise the substitute branch without needing a real DOM.
    """
    from src.apply.adapters._labels import FieldFill

    def _plan(html, profile, boards_api_schema=None):
        if not has_upload:
            return []
        return [
            FieldFill(
                selector='input[type="file"][name="resume"]',
                strategy="upload",
                value=RESUME_SENTINEL,
                label="Resume",
                required=True,
                source="label_scan",
            )
        ]

    import src.apply.adapters.greenhouse as gh
    monkeypatch.setattr(gh, "plan_form_fill", _plan)


def test_greenhouse_rejects_empty_path(tmp_path, monkeypatch):
    """RED: when resume_path=Path(""), Path("") == PosixPath(".") which is
    truthy. Before H15 upload_path becomes Path("") → set_input_files("."),
    which Playwright rejects. After H15, we detect empty-stem and fall back
    to DOCX or fail cleanly.
    """
    _stub_plan(monkeypatch, has_upload=True)

    # Only DOCX available.
    docx = tmp_path / "resume.docx"
    docx.write_bytes(b"docx")

    adapter = GreenhouseAdapter()
    page = _FakePage()
    ctx = _Ctx(tmp_path, resume_path=Path(""), resume_docx_path=docx)

    result = adapter.apply(page, ctx)

    # The set_input_files call MUST NOT be with an empty / '.' path.
    for sel, path in page.set_input_files_calls:
        assert str(path) not in ("", "."), (
            f"H15: set_input_files called with truthy-empty path: {path!r}"
        )
        # If a call did happen, it must reference the DOCX file (the fallback).
        assert str(path).endswith("resume.docx"), (
            f"H15: set_input_files did not fall back to DOCX: got {path!r}"
        )


def test_greenhouse_fails_cleanly_when_no_resume_paths_available(tmp_path, monkeypatch):
    """When both resume_path and resume_docx_path are unusable (empty or
    None), the adapter must return failed(reason=no_resume_available), NOT
    call set_input_files with an empty path.
    """
    _stub_plan(monkeypatch, has_upload=True)

    adapter = GreenhouseAdapter()
    page = _FakePage()
    ctx = _Ctx(tmp_path, resume_path=Path(""), resume_docx_path=None)

    result = adapter.apply(page, ctx)

    # No set_input_files call should have fired.
    assert not page.set_input_files_calls, (
        f"H15: set_input_files was invoked despite no valid resume path: "
        f"{page.set_input_files_calls}"
    )
    assert result.status == "failed"
    assert result.reason == "no_resume_available"
