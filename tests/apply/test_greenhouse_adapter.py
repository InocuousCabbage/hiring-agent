"""tests/apply/test_greenhouse_adapter.py — S8 RED tests for the Greenhouse adapter.

Every test here maps directly to a bullet in S8 spec §TDD test scaffolding.
The tests split into three tiers:

    * Detect + planner (pure) — no Playwright, no I/O, deterministic.
    * Label helper preflight — Boards API URL parser + httpx mocks.
    * Driver (Playwright) — uses a MockPage that spies on locator/select_option
      calls so we can assert on scoping, kwarg discipline, and lifecycle.

Landmine mapping:
    * L1: `test_apply_never_matches_confirmation_by_text_alone`.
    * L2/L11: `test_planner_resolves_via_label_scan_not_first_match`,
      `test_no_first_match_selector_in_planner_output`.
    * L3: `test_submit_selector_is_scoped_to_form`.
    * L4: `test_select_option_uses_label_kwarg`,
      `test_planner_uses_select_option_by_label_strategy`.
    * L5: `test_browser_closed_on_success`, `test_browser_closed_on_exception`.
    * L6: `test_no_datetime_utcnow_in_module`, `test_submitted_at_is_utc_iso`.
    * L7: `test_no_field_values_in_log_output`.
    * L10: `test_field_absent_returns_review_required_for_required_field`,
      `test_field_absent_skips_optional_field`.

Note on cross-shard S2/S5/S9 contracts:
    - S2's `ApplyResult` / `ApplyContext` are duck-typed via SimpleNamespace here;
      the shard defines a local `src.apply.types` shim so runtime object
      construction still works. At merge time the shim converges with S2.
    - S5's `DedupDB` / S9's `captcha.detect` are duck-typed via SimpleNamespace
      and MagicMock respectively.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.apply.adapters._labels import FieldFill  # noqa: E402
from src.apply.adapters.greenhouse import (  # noqa: E402
    GreenhouseAdapter,
    _extract_board_token_and_job_id,
    _fetch_boards_api,
    plan_form_fill,
)


# ── Fixture loader ────────────────────────────────────────────────────────────
FIXTURES = ROOT / "tests" / "fixtures" / "apply"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_boards_api() -> dict:
    return json.loads((FIXTURES / "greenhouse_boards_api.json").read_text(encoding="utf-8"))


# ── Profile / context builders ────────────────────────────────────────────────


def _profile(
    *,
    first: str = "Ada",
    last: str = "Lovelace",
    email: str = "ada@example.io",
    phone: str | None = "+1-555-0101",
    linkedin: str | None = "https://linkedin.com/in/ada",
) -> SimpleNamespace:
    """Duck-typed CandidateProfile (S1 is not in this branch)."""
    return SimpleNamespace(
        name=SimpleNamespace(first=first, last=last, full=f"{first} {last}"),
        contact=SimpleNamespace(
            email=email,
            phone=phone,
            linkedin_url=linkedin,
            portfolio_url=None,
            github_url=None,
        ),
        address=SimpleNamespace(
            line1=None, city=None, state=None, postal=None, country=None
        ),
        work_authorization=SimpleNamespace(us_authorized=True, requires_sponsorship=False),
        eeo=SimpleNamespace(
            gender=None,
            race_ethnicity=None,
            veteran_status=None,
            disability_status=None,
            pronouns=None,
        ),
        compensation=SimpleNamespace(
            desired_salary_usd=None, earliest_start_date=None, willing_to_relocate=None
        ),
        references=(),
    )


def _config(**overrides) -> dict:
    cfg = {
        "mode": "review",
        "dry_run": False,
        "timeout_seconds": 30,
        "navigation_retries": 2,
        "rate_limit_per_ats_per_day": 10,
        "screenshot_dir": None,   # set per-test to tmp_path
        "trace_dir": None,        # set per-test to tmp_path
        "captcha_action": "escalate",
    }
    cfg.update(overrides)
    return cfg


def _dedup(
    *,
    was_applied: bool = False,
    count_today: int = 0,
    soft_warn: list | None = None,
):
    d = MagicMock(name="DedupDB")
    d.was_applied.return_value = was_applied
    d.count_today.return_value = count_today
    d.soft_warn_check.return_value = soft_warn or []
    d.record = MagicMock()
    return d


def _job(url: str = "https://boards.greenhouse.io/acme/jobs/4123456") -> dict:
    return {
        "url": url,
        "apply_url": url,
        "company": "Acme",
        "role": "Software Engineer",
        "ats": "greenhouse",
    }


def _ctx(
    *,
    tmp_path: Path,
    profile=None,
    config=None,
    dedup=None,
    captcha=None,
    resume_path: Path | None = None,
    job: dict | None = None,
    mode: str = "review",
    dry_run: bool = False,
):
    """Build a duck-typed ApplyContext for the driver."""
    cfg = config or _config()
    cfg["mode"] = mode
    cfg["dry_run"] = dry_run
    cfg["screenshot_dir"] = str(tmp_path / "screenshots")
    cfg["trace_dir"] = str(tmp_path / "traces")
    (tmp_path / "screenshots").mkdir(exist_ok=True)
    (tmp_path / "traces").mkdir(exist_ok=True)

    if resume_path is None:
        resume_path = tmp_path / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 minimal")

    return SimpleNamespace(
        profile=profile or _profile(),
        job=job or _job(),
        config=cfg,
        dedup=dedup or _dedup(),
        captcha_detector=captcha or MagicMock(return_value=None),
        resume_path=resume_path,
        cover_letter_path=None,
        applicant="ada@example.io",
        mode=mode,
        dry_run=dry_run,
        storage_state=None,
        session_factory=None,
    )


# ── Mock Page: spies on locator calls, forwards to fixture HTML ──────────────


class _MockLocator:
    """Tracks click/wait_for/count/fill/set_input_files/scoping calls."""

    _global_calls: list[tuple[str, str, tuple, dict]] = []

    def __init__(self, page: "_MockPage", selector: str):
        self.page = page
        self.selector = selector
        # `first` returns self for chaining; matches Playwright API surface.
        self.first = self

    def _record(self, method: str, *args, **kwargs) -> None:
        _MockLocator._global_calls.append((self.selector, method, args, kwargs))
        self.page.locator_calls.append((self.selector, method, args, kwargs))

    def count(self) -> int:
        self._record("count")
        return self.page.selector_present(self.selector)

    def click(self, *args, **kwargs):
        self._record("click", *args, **kwargs)
        # Simulate navigation on submit-scoped clicks.
        if "form#application_form" in self.selector and "Submit" in self.selector:
            self.page._submit_clicked = True
            self.page._url = self.page._post_submit_url
            self.page._html = self.page._post_submit_html

    def fill(self, value, *args, **kwargs):
        self._record("fill", value, *args, **kwargs)

    def check(self, *args, **kwargs):
        self._record("check", *args, **kwargs)

    def set_input_files(self, paths, *args, **kwargs):
        self._record("set_input_files", paths, *args, **kwargs)

    def wait_for(self, *args, **kwargs):
        self._record("wait_for", *args, **kwargs)
        state = kwargs.get("state", "visible")
        timeout = kwargs.get("timeout", 30_000)
        # Simulate confirmation marker absence based on current html.
        if state == "visible" and self.page.confirmation_marker_present() is False:
            raise TimeoutError(f"waiting for {self.selector!r} ({state}, {timeout}ms) timed out")

    def screenshot(self, path=None, **kwargs):
        self._record("screenshot", path=path, **kwargs)
        if path:
            Path(path).write_bytes(b"PNG")


class _MockPage:
    """Minimal Playwright.Page double.

    - Tracks .goto, .locator, .fill, .select_option, .screenshot, .content, .url.
    - Renders `_html` initially (form fixture). After a scoped submit click,
      switches to `_post_submit_html` and `_post_submit_url`.
    - `_missing_selectors` lets a test simulate absent inputs (L10).
    """

    def __init__(
        self,
        *,
        html: str,
        url: str = "https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html: str | None = None,
        post_submit_url: str | None = None,
        missing_selectors: tuple[str, ...] = (),
    ):
        self._html = html
        self._url = url
        self._post_submit_html = post_submit_html if post_submit_html is not None else html
        self._post_submit_url = post_submit_url if post_submit_url is not None else url
        self._missing = set(missing_selectors)
        self._submit_clicked = False
        self.locator_calls: list[tuple[str, str, tuple, dict]] = []
        self.select_option_calls: list[tuple[str, tuple, dict]] = []
        self.goto_calls: list[str] = []
        self.set_input_files_calls: list[tuple[str, Path | str]] = []
        self.close_called = False

    # Playwright API surface used by the adapter --------------------------------
    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self._url = url

    def content(self) -> str:
        return self._html

    @property
    def url(self) -> str:
        return self._url

    def locator(self, selector: str) -> _MockLocator:
        return _MockLocator(self, selector)

    def select_option(self, selector: str, *args, **kwargs):
        self.select_option_calls.append((selector, args, kwargs))

    def fill(self, selector, value, **kwargs):
        self.locator_calls.append((selector, "fill_direct", (value,), kwargs))

    def check(self, selector, **kwargs):
        self.locator_calls.append((selector, "check_direct", (), kwargs))

    def set_input_files(self, selector, files, **kwargs):
        self.set_input_files_calls.append((selector, files))
        self.locator_calls.append((selector, "set_input_files_direct", (files,), kwargs))

    def screenshot(self, path=None, **kwargs):
        if path:
            Path(path).write_bytes(b"PNG")

    def close(self):
        self.close_called = True

    # Introspection used by _MockLocator ----------------------------------------
    def selector_present(self, selector: str) -> int:
        for missing in self._missing:
            if missing in selector:
                return 0
        # A crude count check — sufficient for driver tests.
        return 1

    def confirmation_marker_present(self) -> bool:
        return "application-confirmation" in self._html


# ── Reset the class-level spy between tests ──────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_mock_locator_calls():
    _MockLocator._global_calls.clear()
    yield
    _MockLocator._global_calls.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Detect
# ═══════════════════════════════════════════════════════════════════════════════


def test_detect_matches_canonical_hostname() -> None:
    assert GreenhouseAdapter().detect("https://boards.greenhouse.io/acme/jobs/12345") is True


def test_detect_matches_new_hostname() -> None:
    assert (
        GreenhouseAdapter().detect("https://job-boards.greenhouse.io/acme/jobs/12345")
        is True
    )


def test_detect_returns_false_for_lever() -> None:
    assert GreenhouseAdapter().detect("https://jobs.lever.co/acme/uuid") is False


def test_detect_is_case_insensitive() -> None:
    assert GreenhouseAdapter().detect("https://BOARDS.GREENHOUSE.IO/acme/jobs/12345") is True


def test_detect_never_raises_on_garbage() -> None:
    a = GreenhouseAdapter()
    # None-safe: adapter should not crash on obviously bad input.
    assert a.detect("") is False
    assert a.detect("not-a-url") is False


def test_adapter_name_and_domains_frozen() -> None:
    a = GreenhouseAdapter()
    assert a.name == "greenhouse"
    assert a.domains == ("boards.greenhouse.io", "job-boards.greenhouse.io")


# ═══════════════════════════════════════════════════════════════════════════════
# Planner (pure function)
# ═══════════════════════════════════════════════════════════════════════════════


def test_planner_is_pure() -> None:
    html = _load_html("greenhouse_form.html")
    p = _profile()
    a = plan_form_fill(html, p)
    b = plan_form_fill(html, p)
    assert a == b


def test_planner_fills_first_name_from_profile() -> None:
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile(first="Ada"))
    firsts = [f for f in fills if re.search(r"first\s*name", f.label, re.IGNORECASE)]
    assert firsts
    assert firsts[0].value == "Ada"
    assert firsts[0].strategy == "fill"


def test_planner_resolves_via_label_scan_not_first_match() -> None:
    """L2/L11: two `select[name*="answers_attributes"]` in the fixture; the
    planner MUST resolve the visa-sponsorship question by label, not by
    positional first-match.
    """
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile())
    visa = [
        f
        for f in fills
        if re.search(r"visa\s*sponsorship", f.label, re.IGNORECASE)
    ]
    assert visa
    # Should target the second answers_attributes select (index _1_).
    assert "answers_attributes_1_answer" in visa[0].selector


def test_planner_uses_select_option_by_label_strategy() -> None:
    """L4: every select uses label-based strategy, never positional value."""
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile())
    selects = [f for f in fills if "answers_attributes" in f.selector]
    assert selects
    for f in selects:
        assert f.strategy == "select_option_by_label"


def test_planner_prefers_boards_api_when_provided() -> None:
    html = _load_html("greenhouse_form.html")
    schema = _load_boards_api()
    fills = plan_form_fill(html, _profile(), boards_api_schema=schema)
    # Every produced fill (or at least the primary set) must carry boards_api source.
    assert any(f.source == "boards_api" for f in fills)
    non_boards = [f for f in fills if f.source != "boards_api"]
    # The schema covers every input in our fixture, so no fallback should be needed.
    assert non_boards == []


def test_planner_falls_back_to_label_scan_when_no_schema() -> None:
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile(), boards_api_schema=None)
    assert fills
    assert all(f.source == "label_scan" for f in fills)


def test_planner_marks_required_fields() -> None:
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile())
    resumes = [f for f in fills if "resume" in f.label.lower()]
    assert resumes
    assert resumes[0].required is True


def test_planner_skips_optional_missing_profile_fields() -> None:
    """Profile has no LinkedIn URL -> planner produces NO FieldFill for it."""
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile(linkedin=None))
    linkedin = [f for f in fills if "linkedin" in f.label.lower()]
    assert linkedin == []


def test_no_first_match_selector_in_planner_output() -> None:
    """L2/L11: no fill's selector uses the bare positional first-match idiom."""
    html = _load_html("greenhouse_form.html")
    fills = plan_form_fill(html, _profile())
    for f in fills:
        assert not f.selector.startswith("select[name*="), (
            f"first-match selector escaped into planner output: {f.selector}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Boards API preflight
# ═══════════════════════════════════════════════════════════════════════════════


def test_boards_api_extracts_token_and_id() -> None:
    token, job_id = _extract_board_token_and_job_id(
        "https://boards.greenhouse.io/acme/jobs/4123456"
    )
    assert token == "acme"
    assert job_id == "4123456"


def test_boards_api_extracts_from_new_hostname() -> None:
    token, job_id = _extract_board_token_and_job_id(
        "https://job-boards.greenhouse.io/acme/jobs/9999"
    )
    assert token == "acme"
    assert job_id == "9999"


def test_boards_api_returns_none_on_404() -> None:
    with patch("src.apply.adapters.greenhouse.httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=404)
        assert (
            _fetch_boards_api("https://boards.greenhouse.io/acme/jobs/4123456") is None
        )


def test_boards_api_returns_none_on_timeout() -> None:
    import httpx  # noqa: WPS433 — inside test scope

    with patch(
        "src.apply.adapters.greenhouse.httpx.get",
        side_effect=httpx.TimeoutException("boom"),
    ):
        assert (
            _fetch_boards_api("https://boards.greenhouse.io/acme/jobs/4123456") is None
        )


def test_boards_api_returns_none_on_parse_error() -> None:
    with patch("src.apply.adapters.greenhouse.httpx.get") as mock_get:
        resp = MagicMock(status_code=200)
        resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = resp
        assert (
            _fetch_boards_api("https://boards.greenhouse.io/acme/jobs/4123456") is None
        )


def test_boards_api_returns_dict_on_200() -> None:
    with patch("src.apply.adapters.greenhouse.httpx.get") as mock_get:
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 4123456}
        mock_get.return_value = resp
        out = _fetch_boards_api("https://boards.greenhouse.io/acme/jobs/4123456")
        assert isinstance(out, dict) and out["id"] == 4123456


# ═══════════════════════════════════════════════════════════════════════════════
# Driver — gates that run BEFORE any browser open
# ═══════════════════════════════════════════════════════════════════════════════


def _run_apply(
    *,
    tmp_path: Path,
    page: _MockPage,
    mode: str = "review",
    dry_run: bool = False,
    dedup=None,
    captcha=None,
    profile=None,
    job: dict | None = None,
):
    adapter = GreenhouseAdapter()
    ctx = _ctx(
        tmp_path=tmp_path,
        profile=profile,
        dedup=dedup,
        captcha=captcha,
        job=job,
        mode=mode,
        dry_run=dry_run,
    )
    return adapter.apply(page, ctx), ctx


def test_dedup_hard_hit_short_circuits_before_browser(tmp_path: Path) -> None:
    """L-gate: was_applied True -> ApplyResult('already_applied'); page.goto never called."""
    page = _MockPage(html=_load_html("greenhouse_form.html"))
    dedup = _dedup(was_applied=True)
    result, _ = _run_apply(tmp_path=tmp_path, page=page, dedup=dedup)

    assert result.status == "already_applied"
    assert page.goto_calls == []


def test_rate_limit_short_circuits(tmp_path: Path, caplog) -> None:
    page = _MockPage(html=_load_html("greenhouse_form.html"))
    dedup = _dedup(count_today=10)
    with caplog.at_level("INFO"):
        result, _ = _run_apply(tmp_path=tmp_path, page=page, dedup=dedup)
    assert result.status == "skipped"
    assert result.reason == "rate_limited"
    # apply.rate_limited event fires
    assert any("apply.rate_limited" in rec.message for rec in caplog.records)
    assert page.goto_calls == []


def test_soft_warn_yields_soft_dup_warn_status(tmp_path: Path) -> None:
    """Soft-warn should surface as soft_dup_warn but still route through review-mode fill."""
    page = _MockPage(html=_load_html("greenhouse_form.html"))
    dedup = _dedup(soft_warn=[{"applied_at": "2025-01-01T00:00:00+00:00"}])
    result, _ = _run_apply(tmp_path=tmp_path, page=page, dedup=dedup)
    assert result.status == "soft_dup_warn"
    # Still filled the form (goto called).
    assert page.goto_calls


# ═══════════════════════════════════════════════════════════════════════════════
# Driver — mode + submit behavior
# ═══════════════════════════════════════════════════════════════════════════════


def test_apply_returns_review_required_in_review_mode(tmp_path: Path) -> None:
    page = _MockPage(html=_load_html("greenhouse_form.html"))
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert result.status == "review_required"
    # Submit MUST NOT be clicked.
    clicks = [c for c in page.locator_calls if c[1] == "click"]
    assert clicks == []


def test_apply_returns_submitted_on_confirmation_marker(tmp_path: Path) -> None:
    """Auto mode + confirmation marker present + URL delta -> submitted."""
    form_html = _load_html("greenhouse_form.html")
    confirm_html = _load_html("greenhouse_confirmation.html")
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert result.status == "submitted"


def test_apply_returns_failed_when_confirmation_marker_absent(tmp_path: Path) -> None:
    """Auto mode + no `.application-confirmation` in post-submit HTML -> failed."""
    form_html = _load_html("greenhouse_form.html")
    # Post-submit renders the SAME form -> no marker.
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=form_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456",
    )
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert result.status == "failed"
    assert "confirmation marker" in (result.reason or "")


def test_apply_never_matches_confirmation_by_text_alone(tmp_path: Path) -> None:
    """L1: `Thank you` text is present, but NO DOM marker + no URL delta -> failed."""
    form_html = _load_html("greenhouse_form.html")
    text_only = "<html><body><p>Thank you for applying</p></body></html>"
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=text_only,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456",
    )
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert result.status != "submitted"
    assert result.status == "failed"


def test_submit_selector_is_scoped_to_form(tmp_path: Path) -> None:
    """L3: submit click uses `form#application_form ... :has-text('Submit Application')`."""
    form_html = _load_html("greenhouse_form.html")
    confirm_html = _load_html("greenhouse_confirmation.html")
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    click_calls = [call for call in _MockLocator._global_calls if call[1] == "click"]
    assert click_calls, "no click call recorded"
    submit_selector, _method, _a, _kw = click_calls[0]
    assert submit_selector.startswith("form#application_form")
    assert "Submit Application" in submit_selector


def test_select_option_uses_label_kwarg(tmp_path: Path) -> None:
    """L4: page.select_option must always be called with label= kwarg."""
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert page.select_option_calls, "no select_option calls recorded"
    for _sel, args, kwargs in page.select_option_calls:
        assert "label" in kwargs, (
            f"select_option called without label= kwarg: args={args} kwargs={kwargs}"
        )
        # Args must NOT carry a positional value.
        assert args == () or all(not isinstance(a, str) for a in args), (
            f"select_option called with positional value: {args}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Driver — field absence handling (L10)
# ═══════════════════════════════════════════════════════════════════════════════


def test_field_absent_returns_review_required_for_required_field(tmp_path: Path) -> None:
    """L10: required field missing -> review_required with descriptive reason."""
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html, missing_selectors=("#first_name",))
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert result.status == "review_required"
    assert "First Name" in (result.reason or "") or "first name" in (result.reason or "").lower()


def test_field_absent_skips_optional_field(tmp_path: Path) -> None:
    """Missing optional field -> continues, no review_required."""
    form_html = _load_html("greenhouse_form.html")
    # Phone is optional in the fixture.
    page = _MockPage(html=form_html, missing_selectors=("#phone",))
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert result.status == "review_required"  # mode=review always returns this
    assert (result.reason or "").lower() != "required field missing: phone"


# ═══════════════════════════════════════════════════════════════════════════════
# Driver — CAPTCHA + dry-run
# ═══════════════════════════════════════════════════════════════════════════════


def test_captcha_short_circuit(tmp_path: Path) -> None:
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    captcha = MagicMock(return_value="recaptcha_v2")
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto", captcha=captcha)
    assert result.status == "captcha_escalated"
    # No submit click.
    clicks = [c for c in _MockLocator._global_calls if c[1] == "click"]
    assert clicks == []


def test_dry_run_never_clicks_submit_even_in_auto_mode(tmp_path: Path, caplog) -> None:
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    with caplog.at_level("INFO"):
        result, _ = _run_apply(
            tmp_path=tmp_path, page=page, mode="auto", dry_run=True
        )
    assert result.status == "review_required"
    assert result.reason == "dry_run"
    clicks = [c for c in _MockLocator._global_calls if c[1] == "click"]
    assert clicks == []
    assert any("apply.dry_run.holding_at_submit" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════════
# Driver — screenshots / traces
# ═══════════════════════════════════════════════════════════════════════════════


def test_screenshot_saved_on_review_required(tmp_path: Path) -> None:
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert result.confirmation_screenshot is not None
    assert Path(result.confirmation_screenshot).exists()


def test_trace_path_populated_on_failure(tmp_path: Path) -> None:
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(
        html=form_html,
        post_submit_html=form_html,
    )
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert result.status == "failed"
    # Trace_path is populated as a Path pointing to a file we create.
    assert result.trace_path is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Landmine discipline — grep-based module checks
# ═══════════════════════════════════════════════════════════════════════════════


def test_no_datetime_utcnow_in_module() -> None:
    """L6: datetime.utcnow() never appears in the greenhouse adapter source."""
    src = (ROOT / "src" / "apply" / "adapters" / "greenhouse.py").read_text(encoding="utf-8")
    assert "datetime.utcnow" not in src


def test_submitted_at_is_utc_iso(tmp_path: Path) -> None:
    form_html = _load_html("greenhouse_form.html")
    confirm_html = _load_html("greenhouse_confirmation.html")
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert result.status == "submitted"
    # ISO 8601 UTC with `+00:00` suffix.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result.submitted_at)
    assert result.submitted_at.endswith("+00:00")


# ═══════════════════════════════════════════════════════════════════════════════
# L7: no PII values in log output
# ═══════════════════════════════════════════════════════════════════════════════


def test_no_field_values_in_log_output(tmp_path: Path, caplog) -> None:
    """L7: sentinel email must NEVER appear in captured log records."""
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    profile = _profile(email="SECRET_LEAK@test.io", phone="+1-555-SECRET")
    with caplog.at_level("DEBUG"):
        _run_apply(tmp_path=tmp_path, page=page, mode="review", profile=profile)
    assert "SECRET_LEAK" not in caplog.text
    assert "SECRET" not in caplog.text or "SECRET_LEAK" not in caplog.text  # sanity
    # Loose sanity — phone digits should not leak either.
    assert "555-SECRET" not in caplog.text


# ═══════════════════════════════════════════════════════════════════════════════
# Dedup record semantics
# ═══════════════════════════════════════════════════════════════════════════════


def test_soft_warn_never_auto_submits(tmp_path: Path) -> None:
    """Spec §13c: soft-warn NEVER auto-submits, regardless of `apply.mode`.

    Even with confirmation marker present in the post-submit HTML, a
    non-empty soft_warn_check must force `status="soft_dup_warn"` and skip
    the submit-click branch.
    """
    form_html = _load_html("greenhouse_form.html")
    confirm_html = _load_html("greenhouse_confirmation.html")
    page = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    dedup = _dedup(soft_warn=[{"applied_at": "2025-01-01T00:00:00+00:00"}])
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="auto", dedup=dedup)
    assert result.status == "soft_dup_warn"
    clicks = [c for c in _MockLocator._global_calls if c[1] == "click"]
    assert clicks == []
    # Dedup MUST NOT record on soft-warn short-circuit (S12 handles the record
    # on operator YES).
    dedup.record.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# L5: browser lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


def test_browser_closed_on_success(tmp_path: Path) -> None:
    """L5: page.close() called on the success path (defense-in-depth even though
    S17/S10 own the outer context)."""
    form_html = _load_html("greenhouse_form.html")
    confirm_html = _load_html("greenhouse_confirmation.html")
    page = _MockPage(
        html=form_html,
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    _run_apply(tmp_path=tmp_path, page=page, mode="auto")
    assert page.close_called is True


def test_browser_closed_on_exception(tmp_path: Path) -> None:
    """L5: when an unexpected exception is raised mid-fill, page.close() STILL
    runs in the finally block AND the adapter returns a `failed` result
    (never propagates the exception)."""
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)

    # Poison `page.locator` mid-flow so a fill call raises.
    real_locator = page.locator
    call_count = {"n": 0}

    def _poisoned(sel: str):
        call_count["n"] += 1
        # Let the first-few locator calls succeed (rate-limit gate etc.
        # don't call page.locator; but content()/goto do); once _execute_fill
        # begins calling locator().count(), poison the second call.
        if call_count["n"] >= 2:
            raise RuntimeError("simulated Playwright crash")
        return real_locator(sel)

    page.locator = _poisoned  # type: ignore[assignment]
    result, _ = _run_apply(tmp_path=tmp_path, page=page, mode="review")
    assert result.status in ("failed", "review_required")
    assert page.close_called is True


# ═══════════════════════════════════════════════════════════════════════════════
# Question matcher — whole-word discipline (low-severity code-review fix)
# ═══════════════════════════════════════════════════════════════════════════════


def test_question_matcher_whole_word() -> None:
    """`_match_question` should NOT fire the `email` mapping on a label like
    'Have you emailed us before?' — the keyword must match as a whole word."""
    from src.apply.adapters.greenhouse import _match_question

    assert _match_question("Have you emailed us before?") is None
    assert _match_question("Email") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Dedup record semantics
# ═══════════════════════════════════════════════════════════════════════════════


def test_records_to_dedup_only_on_submitted(tmp_path: Path) -> None:
    """S12 records after operator YES for review-required rows; S8 only records
    on `submitted` (auto-mode confirmation)."""
    form_html = _load_html("greenhouse_form.html")

    # review path -> NO record
    page1 = _MockPage(html=form_html)
    dedup1 = _dedup()
    _run_apply(tmp_path=tmp_path, page=page1, mode="review", dedup=dedup1)
    dedup1.record.assert_not_called()

    # auto path with confirmation -> record called once
    confirm_html = _load_html("greenhouse_confirmation.html")
    page2 = _MockPage(
        html=form_html,
        url="https://boards.greenhouse.io/acme/jobs/4123456",
        post_submit_html=confirm_html,
        post_submit_url="https://boards.greenhouse.io/acme/jobs/4123456/thanks",
    )
    dedup2 = _dedup()
    _run_apply(tmp_path=tmp_path, page=page2, mode="auto", dedup=dedup2)
    dedup2.record.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT — docx-only-lane fallback + no-resume-available guard (renderer-contract audit)
# .agent/one-big-feature/auto-apply-2026-07-06/05-renderer-contract-audit.md
# ═══════════════════════════════════════════════════════════════════════════════


def _ctx_docx_only(*, tmp_path: Path, resume_docx_path: Path | None):
    """Build a ctx where resume_path=None but resume_docx_path may be set."""
    from types import SimpleNamespace as _NS
    cfg = _config()
    cfg["screenshot_dir"] = str(tmp_path / "screenshots")
    cfg["trace_dir"] = str(tmp_path / "traces")
    (tmp_path / "screenshots").mkdir(exist_ok=True)
    (tmp_path / "traces").mkdir(exist_ok=True)
    return _NS(
        profile=_profile(),
        job=_job(),
        config=cfg,
        dedup=_dedup(),
        captcha_detector=MagicMock(return_value=None),
        resume_path=None,
        resume_docx_path=resume_docx_path,
        cover_letter_path=None,
        applicant="ada@example.io",
        mode="review",
        dry_run=False,
        storage_state=None,
        session_factory=None,
    )


def test_greenhouse_uploads_docx_when_pdf_unavailable(tmp_path: Path) -> None:
    """AUDIT: when render_resume() returns (None, docx), the adapter must
    upload the DOCX instead of the RESUME_SENTINEL literal.
    """
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)
    docx_path = tmp_path / "resume.docx"
    docx_path.write_bytes(b"PK\x03\x04 fake docx")

    adapter = GreenhouseAdapter()
    ctx = _ctx_docx_only(tmp_path=tmp_path, resume_docx_path=docx_path)
    result = adapter.apply(page, ctx)

    # The status is not failed/no_resume — the DOCX was substituted into the
    # form fill, so we hit the normal review-required exit for a filled form.
    assert result.status != "failed"
    # The upload path used must be the DOCX path, not the sentinel literal.
    upload_calls = [c for c in page.set_input_files_calls]
    for _sel, files in upload_calls:
        # Files parameter must NEVER be the RESUME_SENTINEL literal.
        assert "<profile.resume_path>" not in str(files)
    # At least one upload actually happened.
    assert any(str(docx_path) in str(files) for _sel, files in upload_calls), (
        f"Expected {docx_path} in upload calls; got {upload_calls}"
    )


def test_greenhouse_fails_when_no_resume_available(tmp_path: Path) -> None:
    """AUDIT: when both resume_path and resume_docx_path are None, the adapter
    MUST NOT pass the RESUME_SENTINEL literal to page.set_input_files() (which
    would crash Playwright). Instead it returns ApplyResult(status='failed',
    reason='no_resume_available').
    """
    form_html = _load_html("greenhouse_form.html")
    page = _MockPage(html=form_html)

    adapter = GreenhouseAdapter()
    ctx = _ctx_docx_only(tmp_path=tmp_path, resume_docx_path=None)
    result = adapter.apply(page, ctx)

    assert result.status == "failed"
    assert result.reason == "no_resume_available"
    # RESUME_SENTINEL literal must never reach set_input_files.
    for _sel, files in page.set_input_files_calls:
        assert "<profile.resume_path>" not in str(files)
