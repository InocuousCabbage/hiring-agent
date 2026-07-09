"""
tests/conftest.py — the single, repo-wide pytest configuration for the
hiring-agent auto-apply MVP (owned by shard S18).

Design invariants (see spec §Acceptance criteria and §10 landmines):

1. This file is the ONLY conftest.py in the repository — S19 will register the
   `live_ats` marker in pyproject.toml, not in a second conftest.
2. Every fixture that touches production code lazy-imports the symbol INSIDE
   the fixture body, so `pytest --collect-only` never fails on missing
   modules while the parallel writer-subagents are still filling in
   src/apply/**.  RED-state failures surface at test-invocation time, not at
   collection time, keeping collection under the 5-second budget.
3. `MockATSPage` wraps its entire Chromium lifecycle (playwright.start,
   browser, context) in a single try/finally block — the exact shape the
   Greenhouse adapter must mirror (L5).
4. `frozen_now` monkeypatches `datetime.now(timezone.utc)`; it NEVER uses
   `datetime.utcnow` (L6).
5. `capture_logs` ALWAYS installs the S16 scrubber BEFORE beginning capture
   so a downstream regression can never sneak PII into logs through a
   test-only bypass (L7).
6. Every fixture that yields state is either function-scoped or torn down
   via monkeypatch, so no state leaks between tests (blocking review criterion).
7. Fixtures use ONLY placeholder PII (Jane Doe / jane@example.com /
   +1-555-0100 — the same values S1 writes into
   tests/fixtures/apply/profile_valid.yaml).  Real Ben-PII must NEVER
   appear in any fixture.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Repo-relative fixture paths — canonical source lives in tests/apply/_paths.py
# so downstream test modules can import them without depending on `tests/`
# being importable as a package (avoids cross-directory import brittleness).
# ---------------------------------------------------------------------------

from tests.apply._paths import (  # noqa: E402
    FIXTURES,
    GREENHOUSE_BOARDS_API_JSON,
    GREENHOUSE_CONFIRMATION_HTML,
    GREENHOUSE_FORM_HTML,
    PROFILE_VALID_YAML,
    REPO_ROOT,
)


# ---------------------------------------------------------------------------
# MockATSPage — real Playwright Page bound to a Chromium context with
# page.route() interception.  Yields via a fixture that wraps the entire
# triple (playwright.start → chromium.launch → new_context) in ONE
# try/finally block (L5).
# ---------------------------------------------------------------------------


class MockATSPage:
    """
    Real Playwright Page with saved-fixture route interception.

    Every S18-authored test that exercises adapter code against a saved
    HTML/JSON fixture goes through this class — never a hand-rolled Page
    double — so the same Playwright API surface (locator, select_option,
    set_input_files, click) is exercised offline that adapters call in
    production.
    """

    def __init__(self, page: "Any") -> None:  # Page type deferred to runtime
        self._page = page
        self._html_routes: list[tuple[str, Path]] = []
        self._json_routes: list[tuple[str, Path]] = []
        self._nav_map: dict[str, str] = {}

    # ---- registration helpers ------------------------------------------------

    def serve_html(self, url_glob: str, fixture_path: Path) -> None:
        """Register a page.route() handler that returns saved HTML."""
        fixture_path = Path(fixture_path)
        if not fixture_path.exists():
            raise FileNotFoundError(f"MockATSPage.serve_html: missing fixture {fixture_path}")
        body = fixture_path.read_bytes()
        self._html_routes.append((url_glob, fixture_path))

        def _handler(route: "Any") -> None:  # noqa: ANN001
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        self._page.route(url_glob, _handler)

    def serve_json(self, url_glob: str, fixture_path: Path) -> None:
        """Register a page.route() handler that returns saved JSON."""
        fixture_path = Path(fixture_path)
        if not fixture_path.exists():
            raise FileNotFoundError(f"MockATSPage.serve_json: missing fixture {fixture_path}")
        body = fixture_path.read_bytes()
        self._json_routes.append((url_glob, fixture_path))

        def _handler(route: "Any") -> None:  # noqa: ANN001
            route.fulfill(status=200, content_type="application/json", body=body)

        self._page.route(url_glob, _handler)

    def simulate_navigation(self, pre_url: str, post_url: str) -> None:
        """
        Register that on next navigation from pre_url, the browser lands at
        post_url — used to model post-submit URL delta (L1 confirmation check).
        Bound to a route() handler that emits a 302 for pre_url → post_url.
        """
        self._nav_map[pre_url] = post_url

        def _handler(route: "Any") -> None:  # noqa: ANN001
            route.fulfill(status=302, headers={"Location": post_url}, body=b"")

        self._page.route(pre_url, _handler)

    # ---- exposed real Page --------------------------------------------------

    @property
    def page(self) -> "Any":
        """The underlying Playwright Page — real API surface."""
        return self._page


# ---------------------------------------------------------------------------
# Fixture: placeholder-PII CandidateProfile (from S1's profile_valid.yaml).
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_candidate_profile():  # -> src.apply.profile.CandidateProfile
    """
    Loads the canonical placeholder-PII profile authored by S1 at
    tests/fixtures/apply/profile_valid.yaml.  Fixture is function-scoped so
    per-test mutation (frozen dataclass — should raise) never leaks.
    """
    from src.apply.profile import CandidateProfile  # lazy: S1 landing

    if not PROFILE_VALID_YAML.exists():
        pytest.fail(
            f"Expected S1's placeholder-PII profile at {PROFILE_VALID_YAML}; "
            "this fixture cannot construct a profile without it."
        )
    return CandidateProfile.load(PROFILE_VALID_YAML)


# ---------------------------------------------------------------------------
# Fixture: ApplyContext factory.
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_apply_context(sample_candidate_profile, tmp_path):  # -> ApplyContext
    """
    Build a valid ApplyContext with a placeholder Greenhouse job URL,
    tmp_path-scoped resume + cover-letter files, and apply.dry_run=True.
    Re-constructed per-fixture-call so downstream mutation cannot bleed
    between tests (blocking review criterion).
    """
    from src.apply.types import ApplyContext  # lazy: S2 landing

    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.7\n%fixture resume\n")
    cover_letter_path = tmp_path / "cover_letter.pdf"
    cover_letter_path.write_bytes(b"%PDF-1.7\n%fixture cover letter\n")
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()

    job = {
        "url": "https://boards.greenhouse.io/testco/jobs/4000000000",
        "apply_url": "https://boards.greenhouse.io/testco/jobs/4000000000",
        "company": "Testco",
        "role_title": "Software Engineer",
        "ats": "greenhouse",
        "ats_domain": "boards.greenhouse.io",
        "ats_job_id": "4000000000",
    }

    return ApplyContext(
        profile=sample_candidate_profile,
        job=job,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        config={
            "apply": {
                "enabled": True,
                "mode": "review",
                "dry_run": True,
                "allowed_ats": ["greenhouse"],
                "screenshot_dir": str(screenshot_dir),
            }
        },
        applicant="jane",
        dry_run=True,
        mode="review",
    )


# ---------------------------------------------------------------------------
# Fixture: DedupDB seeded with three canonical rows.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dedup_db(tmp_path, sample_candidate_profile):
    """
    Yields a DedupDB(tmp_path/'applied_jobs.db') seeded with three canonical
    prior rows — hard-dup, soft-dup, unrelated — all using placeholder-PII
    values.  Row values are wrapped in ApplyResult objects so the seed path
    exercises the same code the production shard uses.
    """
    from src.apply.dedup import DedupDB  # lazy: S5 landing
    from src.apply.types import ApplyResult  # lazy: S2 landing

    db_path = tmp_path / "applied_jobs.db"
    db = DedupDB(db_path)
    applicant = "jane"

    # Row 1 — HARD-DUP candidate: same (company, ats_domain, ats_job_id)
    # as the sample job URL used across the suite.  A second .record with
    # the same triple must be blocked by the UNIQUE constraint.
    hard = ApplyResult(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/testco/jobs/4000000000",
        application_id="app_hard_dup",
        submitted_at="2026-06-30T12:00:00+00:00",
    )
    db.record(
        result=hard,
        applicant=applicant,
        company="Testco",
        role_title="Software Engineer",
        job_url="https://boards.greenhouse.io/testco/jobs/4000000000",
    )

    # Row 2 — SOFT-DUP candidate: same company_normalized + role_title_normalized
    # but different ATS/id.  soft_warn_check must return this row.
    soft = ApplyResult(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/testco/jobs/4111111111",
        application_id="app_soft_dup",
        submitted_at="2026-06-25T09:00:00+00:00",
    )
    db.record(
        result=soft,
        applicant=applicant,
        company="Testco",
        role_title="Senior Software Engineer",
        job_url="https://boards.greenhouse.io/testco/jobs/4111111111",
    )

    # Row 3 — UNRELATED: totally different company + role.  Must NOT hit
    # any of the dedup guards for the primary sample job.
    other = ApplyResult(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/othercorp/jobs/9000000000",
        application_id="app_unrelated",
        submitted_at="2026-06-10T18:00:00+00:00",
    )
    db.record(
        result=other,
        applicant=applicant,
        company="Othercorp",
        role_title="Data Analyst",
        job_url="https://boards.greenhouse.io/othercorp/jobs/9000000000",
    )

    return db


# ---------------------------------------------------------------------------
# Fixture: MagicMock Gmail client with sane defaults.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gmail_client() -> MagicMock:
    """
    MagicMock stand-in for src.gmail.client.GmailClient with a small set of
    sane return values for methods S12/S13 call.  Records call arguments
    for assertion.
    """
    client = MagicMock(name="mock_gmail_client")
    client.search.return_value = []
    client.get_or_create_label.return_value = {"id": "Label_1", "name": "hiring-agent/apply/pending"}
    client.list_labels.return_value = [
        {"id": "Label_1", "name": "hiring-agent/apply/pending"},
        {"id": "Label_2", "name": "hiring-agent/apply/submitted"},
        {"id": "Label_3", "name": "hiring-agent/apply/declined"},
    ]
    client.apply_label.return_value = None
    client.remove_label.return_value = None
    client.send.return_value = {"id": "msg_1", "threadId": "thread_1"}
    client.send_immediate.return_value = {"id": "msg_2", "threadId": "thread_2"}
    return client


# ---------------------------------------------------------------------------
# Fixture: in-memory keyring backend for S6 credential tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_keyring(monkeypatch) -> dict:
    """
    Monkeypatches keyring.set_password / get_password / delete_password to an
    in-memory dict.  Enforces service-name pattern `hiring-agent.<ats>.<user>`
    on every set/get by raising a pytest.fail if a non-conforming service
    name reaches the mock (blocking review criterion — service-name pattern
    audit).

    Uses monkeypatch so state auto-tears-down at fixture exit (function scope).
    """
    import keyring  # third-party; loaded lazily to avoid collect-time cost

    store: dict[tuple[str, str], str] = {}
    _pattern = re.compile(r"^hiring-agent\.[a-z][a-z0-9\-]*(\.[A-Za-z0-9_\-.]+)?$")

    def _check_service(service: str) -> None:
        if not _pattern.match(service):
            pytest.fail(
                f"mock_keyring: service name {service!r} does not match "
                "'hiring-agent.<ats>.<user>' (nor the bootstrap 'hiring-agent.<name>' form)."
            )

    def _set(service: str, username: str, password: str) -> None:
        _check_service(service)
        store[(service, username)] = password

    def _get(service: str, username: str) -> str | None:
        _check_service(service)
        return store.get((service, username))

    def _delete(service: str, username: str) -> None:
        _check_service(service)
        store.pop((service, username), None)

    monkeypatch.setattr(keyring, "set_password", _set)
    monkeypatch.setattr(keyring, "get_password", _get)
    monkeypatch.setattr(keyring, "delete_password", _delete)

    # Also patch delete_password if downstream tests want it (optional).
    return store


# ---------------------------------------------------------------------------
# Fixture: MockATSPage — real Chromium Page with page.route() interception.
# Chromium teardown is wrapped in ONE try/finally block covering the entire
# triple (L5).
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ats_page() -> Iterator[MockATSPage]:
    """
    Yields a MockATSPage bound to a real Chromium context — headless.  The
    ENTIRE (sync_playwright().start → chromium.launch → new_context → new_page)
    triple — including the driver-start call itself — is wrapped in ONE
    outer try/finally block so a mid-setup failure at any step never leaks
    a Chromium driver process (L5 — the exact shape the Greenhouse adapter
    must mirror).
    """
    from playwright.sync_api import sync_playwright  # lazy import

    playwright = None
    browser = None
    context = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        yield MockATSPage(page)
    finally:
        # Nested try/finally so a close-time exception in the inner
        # resource does not abort teardown of the outer resource.
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                if playwright is not None:
                    playwright.stop()


# ---------------------------------------------------------------------------
# Fixture: frozen_now — patches datetime.now(timezone.utc) inside modules
# under test.  Never uses datetime.utcnow (L6).
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_now(monkeypatch):
    """
    Returns a helper that patches `datetime.now(timezone.utc)` inside one or
    more modules under test.  Usage:

        frozen_now("2026-07-07T12:00:00+00:00", "src.apply.review", "src.apply.retention")

    Patches by installing a small `_FrozenDatetime` class in the target
    module's namespace under the name `datetime` — so any code inside that
    module calling `datetime.now(timezone.utc)` receives the frozen instant.

    Never touches `datetime.utcnow` (L6).
    """

    def _apply(iso: str, *modules: str) -> datetime:
        fixed = datetime.fromisoformat(iso)
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                # L6 — we freeze `datetime.now(timezone.utc)` ONLY.  Any
                # module that reaches for `datetime.utcnow()` is caught by
                # the L6 grep test in test_greenhouse_adapter.py; we do not
                # override utcnow here to avoid endorsing that call site.
                #
                # Naive-datetime ambiguity: stdlib `datetime.now(None)`
                # returns naive-local-time; here we would return the
                # UTC-instant-with-tzinfo-stripped which is semantically
                # different.  To surface a mixed-call-style bug (a src/apply
                # module that mixes `datetime.now()` and `datetime.now(utc)`)
                # rather than silently mask it, we raise on the naive path
                # inside frozen tests — production code must always pass
                # `timezone.utc`.
                if tz is None:
                    raise AssertionError(
                        "frozen_now: naive datetime.now() call detected inside a "
                        "test module using the freeze.  Every apply-* module MUST "
                        "call datetime.now(timezone.utc); a naive call is an L6-"
                        "adjacent smell."
                    )
                return fixed.astimezone(tz)

        for module_name in modules:
            module = importlib.import_module(module_name)
            # raising=False was previously used to allow patching modules
            # that don't yet import `datetime` at all.  That silently no-ops
            # against a module whose author renamed the import to `dt`.
            # Prefer raising=True so a wrong module name/import shape
            # surfaces immediately.
            if not hasattr(module, "datetime"):
                raise AttributeError(
                    f"frozen_now: module {module_name!r} does not expose a "
                    "`datetime` binding; the module must import via "
                    "`from datetime import datetime, timezone` for the freeze "
                    "to take effect."
                )
            monkeypatch.setattr(module, "datetime", _FrozenDatetime)

        return fixed

    return _apply


# ---------------------------------------------------------------------------
# Fixture: capture_logs — installs the S16 scrubber BEFORE capture (L7).
# ---------------------------------------------------------------------------


@dataclass
class _CapturedLogs:
    """Container for captured structlog events with PII-scan helper."""

    entries: list[dict] = field(default_factory=list)

    def assert_no_pii(self, profile) -> None:
        """
        Walk captured events RECURSIVELY; assert none contains PII derived
        from the supplied CandidateProfile.  Every scalar (str, int, bytes,
        exception repr, dataclass repr, etc.) inside every event dict — at
        any depth — is stringified and case-insensitively grep'd for the
        PII substrings:
          - profile.contact.email
          - profile.contact.phone (both the raw string form AND the
            digits-only substring)
          - profile.contact.linkedin_url  (may be None)
          - profile.address.line1        (may be None)
          - profile.name.first
          - profile.name.last

        Container types (dict, list, tuple, set) are walked recursively —
        naive `isinstance(v, str)`-only walks silently miss PII inside
        `filled_fields={"email": "..."}` and inside exception reprs
        containing candidate values.
        """
        email = getattr(profile.contact, "email", None)
        phone = getattr(profile.contact, "phone", None)
        linkedin = getattr(profile.contact, "linkedin_url", None)
        first = getattr(profile.name, "first", None)
        last = getattr(profile.name, "last", None)
        address_line1 = getattr(getattr(profile, "address", None), "line1", None)
        digits = "".join(c for c in (phone or "") if c.isdigit()) or None

        # Both the raw phone string AND the digits-only substring are
        # checked — a leak of "+1-555-0100" (the natural log format) does
        # not contain "15550100" as a substring.
        raw_forbidden = [
            v for v in (email, phone, linkedin, first, last, address_line1, digits) if v
        ]
        # Case-insensitive comparison — an adapter that upper-cases values
        # before logging (e.g. via `str.title()`) would sneak past a
        # case-sensitive check.
        forbidden_lower = [str(v).lower() for v in raw_forbidden]

        def _walk(obj: Any, path: str) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, (list, tuple, set, frozenset)):
                for i, v in enumerate(obj):
                    _walk(v, f"{path}[{i}]")
            elif obj is None or isinstance(obj, bool):
                return
            else:
                # Every non-container leaf: stringify and search.
                s_lower = str(obj).lower()
                for needle_lower, needle_raw in zip(forbidden_lower, raw_forbidden):
                    if needle_lower and needle_lower in s_lower:
                        raise AssertionError(
                            f"PII leak at {path}: substring {needle_raw!r} appeared "
                            f"unscrubbed in value {obj!r}"
                        )

        for i, evt in enumerate(self.entries):
            _walk(evt, f"events[{i}]")


@pytest.fixture
def capture_logs() -> Iterator[_CapturedLogs]:
    """
    Captures structlog events that flow THROUGH the S16 scrubber.

    Design note (L7 anchor): `structlog.testing.capture_logs()` REPLACES
    the configured processor chain with a bare `LogCapture` — that means
    events emitted inside its `with` block bypass the scrubber entirely.
    Using it here would silently pass every PII-regression test.

    Instead we:
      1. Install the S16 scrubber (configures the global processor chain).
      2. Snapshot the resulting processor list.
      3. Insert a custom `_capture_processor` immediately BEFORE the tail
         renderer, so every event dict flows: user → scrubber → capture →
         renderer.  The capture processor appends a shallow copy of the
         event dict to `captured.entries` — post-scrub.
      4. On fixture exit, restore the original processor chain (so
         subsequent tests are not permanently in "capture" mode).

    `captured.entries` accumulates in-place during the test — a mid-test
    `assert_no_pii(profile)` sees events as they land.
    """
    import structlog

    # ALWAYS install the scrubber first (L7).  Lazy import: if S16 has not
    # landed yet, this fixture surfaces a clean RED-state failure.
    from src.apply.logging import install_scrubber  # lazy: S16 landing
    install_scrubber()

    captured = _CapturedLogs()
    entries_ref = captured.entries  # live-list alias for the closure below

    def _capture_processor(logger, method_name, event_dict):
        # Append AFTER the scrubber has already redacted the dict — a copy
        # so mutations by downstream renderers do not race with test
        # assertions.
        entries_ref.append(dict(event_dict))
        return event_dict

    current_cfg = structlog.get_config()
    old_processors = list(current_cfg.get("processors") or [])
    if not old_processors:
        # No processors configured — degenerate case, install just the
        # capture processor.  Downstream PII assertions still work.
        new_processors = [_capture_processor]
    else:
        # Insert capture immediately before the tail (renderer).  This
        # guarantees the scrubber (which sits earlier in the chain) has
        # already run on every event we capture.
        new_processors = old_processors[:-1] + [_capture_processor] + old_processors[-1:]

    structlog.configure(processors=new_processors)
    try:
        yield captured
    finally:
        # Restore prior processor chain so this fixture's install does
        # not leak into subsequent tests.
        structlog.configure(processors=old_processors)


# ---------------------------------------------------------------------------
# Fixture: apply_settings — dict with the §4.7 config-key shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def apply_settings(tmp_path) -> dict:
    """
    Returns a dict pre-populated with the §4.7 `apply.*` shape.  Every test
    that touches config runs in dry-run mode by default (apply.dry_run=True)
    so a leaky adapter cannot submit a real application in the offline suite.
    """
    return {
        "apply": {
            "enabled": True,
            "mode": "review",
            "allowed_ats": ["greenhouse"],
            "long_tail": "none",
            "dry_run": True,
            "timeout_seconds": 90,
            "navigation_retries": 2,
            "rate_limit_per_ats_per_day": 10,
            "review_timeout_hours": 72,
            "review_reping_hours": 24,
            "retention_days": 30,
            "screenshot_dir": str(tmp_path / "state" / "screenshots"),
            "trace_dir": str(tmp_path / "state" / "traces"),
            "storage_state_dir": str(tmp_path / "config" / "credentials" / "apply"),
            "dedup_db_path": str(tmp_path / "state" / "applied_jobs.db"),
            "captcha_action": "escalate",
            "captcha_transport": "browserbase",
            "profile_path": str(PROFILE_VALID_YAML),
            "gmail_label_prefix": "hiring-agent/apply",
            "fast_path_recipient": "jane@example.com",
            "browserbase": {
                "enabled": True,
                "solve_captchas": True,
                "proxies": True,
                "block_ads": True,
            },
        }
    }


# ---------------------------------------------------------------------------
# Fixture: run_seam_config — top-level config the S17 seam integration test
# passes to run_pipeline.  Reuses apply_settings and stitches in the
# minimum sibling keys the seam expects.
# ---------------------------------------------------------------------------


def apply_context_with_mode(ctx, *, mode: str, dry_run: bool):
    """
    Build a new ApplyContext with `mode` + `dry_run` OVERRIDDEN on BOTH the
    top-level dataclass fields AND the mirrored `config["apply"]` keys, so
    an adapter that reads `ctx.mode` and an adapter that reads
    `ctx.config["apply"]["mode"]` observe the same value.  Without this
    helper, `dataclasses.replace(ctx, mode="auto")` leaves
    `ctx.config["apply"]["mode"]` at "review", silently exercising the
    review-path in what looks like an auto-mode test.
    """
    from dataclasses import replace  # imported lazily; ApplyContext type deferred
    new_config = copy.deepcopy(ctx.config)
    new_config.setdefault("apply", {})
    new_config["apply"]["mode"] = mode
    new_config["apply"]["dry_run"] = dry_run
    return replace(ctx, mode=mode, dry_run=dry_run, config=new_config)


# ---------------------------------------------------------------------------
# Fixture: main_root_with_config — redirect main.ROOT to a tmp scratch dir
# populated with real settings.yaml + project_bank.yaml (with apply.enabled
# forced to False). Extracted (Phase 5 iter-2, finding #11) to remove the
# 5-line block that was duplicated across test_review_fixes.py and
# test_full_pipeline.py (three call sites). Also fixes finding M18 (tests
# were config-dependent on apply.enabled=false — this fixture forces it).
# ---------------------------------------------------------------------------


@pytest.fixture
def main_root_with_config():
    """Callable that, when invoked with (monkeypatch, tmp_path), sets
    main.ROOT to tmp_path and stages real settings.yaml + project_bank.yaml
    beneath it (with apply.enabled forced to False, independent of the
    shipped setting — so a config flip in settings.yaml can never silently
    reshape the digest-send branch these tests exercise).

    Returns tmp_path for chaining.
    """
    import yaml as _yaml_local

    def _apply(monkeypatch, tmp_path):
        import main as main_mod  # noqa: PLC0415 — fixture invocation
        real_root = Path(__file__).parent.parent
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "templates").mkdir(exist_ok=True)
        # Load settings.yaml, force apply.enabled=false, write to tmp.
        # Independent of the shipped default — a future config flip can't
        # silently shift the digest-send branch (fixes finding M18).
        settings_data = _yaml_local.safe_load(
            (real_root / "config" / "settings.yaml").read_text()
        )
        settings_data.setdefault("apply", {})
        settings_data["apply"]["enabled"] = False
        (tmp_path / "config" / "settings.yaml").write_text(
            _yaml_local.safe_dump(settings_data)
        )
        (tmp_path / "templates" / "project_bank.yaml").write_text(
            (real_root / "templates" / "project_bank.yaml").read_text()
        )
        monkeypatch.setattr(main_mod, "ROOT", tmp_path)
        return tmp_path

    return _apply


@pytest.fixture
def run_seam_config(apply_settings) -> dict:
    """
    Returns a full config dict shaped for the S17 seam-wiring test.  Includes
    the `apply.*` block plus the sibling `gmail`, `pipeline`, and `paths`
    keys the seam consults so run_pipeline() executes end-to-end offline.
    """
    return {
        **apply_settings,
        "gmail": {
            "user_id": "me",
            "credentials_path": "config/credentials/gmail.json",
        },
        "pipeline": {
            "max_jobs_per_run": 5,
            "screenshot_on_failure": True,
        },
        "paths": {
            "state_dir": "state",
            "output_dir": "output",
        },
    }
