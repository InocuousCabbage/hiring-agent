"""Gated live-suite: dry-run against boards.greenhouse.io/greenhouse
(Greenhouse's own demo board — Ben Q17).

Every test in this module is gated by TWO independent guards:
  1. `@pytest.mark.live_ats` — deselected by default via pyproject addopts.
  2. `require_live_env` — HIRING_AGENT_LIVE_ATS env-var opt-in + HEAD probe.

Contracts:
  - NEVER runs in CI (asserted by shard S21 docs; no workflow file invokes -m live_ats).
  - NEVER hits a real employer (target token guarded to LIVE_TARGET_BOARD=='greenhouse').
  - NEVER clicks final submit (apply.dry_run=True enforced as the FIRST assertion of every test).
  - NEVER writes state/applied_jobs.db (mtime snapshot guard).
  - <= 3 requests to boards.greenhouse.io per test (rate-limit safety on demo endpoint).

`src.*` imports are performed LAZILY inside test bodies so that this module
collects cleanly in a worktree where S8 (GreenhouseAdapter) / S9 (captcha)
/ S4 (browser session) have not yet been merged from their shards. The
default `pytest` invocation deselects every test here before any body runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ------------------------------------------------------------------------
# Module-level target-board constants (acceptance #5).
# ------------------------------------------------------------------------
LIVE_TARGET_BOARD = "greenhouse"
LIVE_TARGET_URL_PREFIX = f"https://boards.greenhouse.io/{LIVE_TARGET_BOARD}"
MAX_REQUESTS_PER_TEST = 3  # rate-limit safety (acceptance #16)
STARTING_LOG_EVENT = "apply.live.starting"  # first log line (acceptance #14)
DEDUP_DB_PATH = Path("state/applied_jobs.db")  # never-touched guard (acceptance #8)


def _getmtime_or_none(path: Path):
    """Return path.stat().st_mtime or None if the path does not exist.

    Used to snapshot state/applied_jobs.db mtime before/after a live run
    without erroring on a fresh checkout that has no DB yet (acceptance #8).
    """
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _assert_target_guard() -> None:
    """Every test calls this to enforce the board-token contract (acceptance #5).

    Any test that tries to redirect the live target away from Greenhouse's demo
    board fails here before touching the network.
    """
    assert LIVE_TARGET_BOARD == "greenhouse", (
        f"LIVE_TARGET_BOARD must be 'greenhouse' (Greenhouse demo board — Ben Q17), "
        f"got {LIVE_TARGET_BOARD!r}. A different token would risk hitting a real employer."
    )
    assert LIVE_TARGET_URL_PREFIX == "https://boards.greenhouse.io/greenhouse", (
        f"LIVE_TARGET_URL_PREFIX must exactly match Greenhouse's demo board, "
        f"got {LIVE_TARGET_URL_PREFIX!r}."
    )


def _assert_dry_run_first(apply_settings) -> None:
    """FIRST assertion in every test — before any network I/O (acceptance #4).

    If apply.dry_run is not True the test fails immediately with a clear message
    rather than silently proceeding to a real submit path.
    """
    assert apply_settings["apply"]["dry_run"] is True, (
        "apply.dry_run must be True in the live suite — live tests NEVER submit. "
        "Fail-fast before any network I/O."
    )


# ========================================================================
# Test 1 — dry-run returns review_required + rate-limit + PII + DB guards.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_dry_run_review_required(
    require_live_env,
    apply_settings,
    sample_apply_context,
    capture_logs,
    sample_candidate_profile,
    tmp_dedup_db,
    tmp_path,
):
    """End-to-end dry-run against boards.greenhouse.io/greenhouse.

    Asserts:
      - apply.dry_run=True (acceptance #4)
      - board token/URL guard (acceptance #5)
      - GreenhouseAdapter.apply(...).status == 'review_required' (acceptance #7)
      - confirmation_screenshot exists and > 1 KB
      - request counter <= 3 (acceptance #16)
      - no PII in captured logs (acceptance #13)
      - state/applied_jobs.db mtime unchanged (acceptance #8)
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    # Snapshot real dedup DB mtime BEFORE any adapter work.
    db_mtime_before = _getmtime_or_none(DEDUP_DB_PATH)

    # Lazy imports — S8/S4 land at final merge; keeps module collectable meanwhile.
    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.browser.session import session

    request_counter = {"n": 0}

    with session(transport="local") as (browser, context, page):
        # Assert SessionContext.transport == "local" per §Contracts consumed.
        assert getattr(context, "transport", "local") == "local", (
            "live tests must use local transport — Browserbase is Phase 3.6 spike, not this shard."
        )
        page.on(
            "request",
            lambda req: request_counter.__setitem__("n", request_counter["n"] + 1),
        )
        result = GreenhouseAdapter().apply(page, sample_apply_context)

    assert result.status == "review_required", (
        f"live dry-run must return status='review_required' (never 'submitted' — "
        f"dry_run halts at pre-submit), got {result.status!r}"
    )

    screenshot = Path(result.confirmation_screenshot)
    assert screenshot.exists() and screenshot.stat().st_size > 1024, (
        f"confirmation_screenshot missing or < 1 KB: {screenshot}"
    )

    assert request_counter["n"] <= MAX_REQUESTS_PER_TEST, (
        f"live suite fired {request_counter['n']} requests to boards.greenhouse.io "
        f"(rate-limit safety limit={MAX_REQUESTS_PER_TEST}) — investigate misuse of demo endpoint."
    )

    capture_logs.assert_no_pii(sample_candidate_profile)

    db_mtime_after = _getmtime_or_none(DEDUP_DB_PATH)
    assert db_mtime_after == db_mtime_before, (
        f"state/applied_jobs.db mtime changed ({db_mtime_before} -> {db_mtime_after}) — "
        f"the live suite MUST NOT write the real dedup DB (use tmp_dedup_db fixture)."
    )


# ========================================================================
# Test 2 — no CAPTCHA expected on the Greenhouse demo board.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_no_captcha_expected(
    require_live_env,
    apply_settings,
    sample_apply_context,
):
    """The demo board should never CAPTCHA. If it does, either Greenhouse changed
    the demo, or S9's detector is false-positive — both need re-audit.
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.captcha import detect
    from src.browser.session import session

    with session(transport="local") as (browser, context, page):
        page.goto(LIVE_TARGET_URL_PREFIX)
        captcha = detect(page)
        assert captcha is None, (
            "unexpected captcha on greenhouse demo board; "
            "re-audit S9 markers and re-record fixture"
        )


# ========================================================================
# Test 3 — meta: default pytest DESELECTS this suite (subprocess check).
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_default_pytest_deselects_this_test():
    """Meta-verify addopts='-m not live_ats' actually deselects live tests.

    Rationale for the @pytest.mark.live_ats decoration on a meta-check:
    the subprocess pytest run is an offline assertion (no network), but keeping
    the decoration means the outer check only runs in the opt-in invocation.
    Removing the decoration would leak this subprocess spawn into every default
    `pytest` run, which is undesirable overhead.
    """
    repo_root = Path(__file__).resolve().parents[3]

    # Ensure the inner subprocess does NOT inherit the opt-in flag, so it
    # exercises the default (deselect) behavior.
    inner_env = dict(os.environ)
    inner_env["HIRING_AGENT_LIVE_ATS"] = ""

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/apply/live/",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        env=inner_env,
    )
    combined = proc.stdout + proc.stderr

    # Positive signal: pytest prints "deselected" in the summary when addopts is honored.
    assert "deselected" in combined.lower(), (
        f"default pytest did NOT deselect live suite (missing 'deselected' in summary). "
        f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # Negative signal: no live test node IDs should appear in the collect output.
    assert "tests/apply/live/test_greenhouse_demo.py::" not in combined, (
        f"default pytest still collected live tests — addopts '-m not live_ats' broken. "
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ========================================================================
# Test 4 — never writes the real state/applied_jobs.db.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_never_writes_real_dedup_db(
    require_live_env,
    apply_settings,
    sample_apply_context,
    tmp_dedup_db,
):
    """Explicit isolation of the DB-mtime assertion (acceptance #8)."""
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.browser.session import session

    mtime_before = _getmtime_or_none(DEDUP_DB_PATH)

    with session(transport="local") as (browser, context, page):
        GreenhouseAdapter().apply(page, sample_apply_context)

    mtime_after = _getmtime_or_none(DEDUP_DB_PATH)
    assert mtime_after == mtime_before, (
        f"state/applied_jobs.db mtime changed ({mtime_before} -> {mtime_after}) — "
        f"live suite must be a no-op on the real dedup DB."
    )


# ========================================================================
# Test 5 — L5 try/finally teardown reaches both closes on mid-fill raise.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_teardown_in_try_finally_L5(
    require_live_env,
    apply_settings,
    sample_apply_context,
    monkeypatch,
):
    """Force the adapter to raise mid-fill (via a monkeypatched attribute that
    raises on the second call) and assert that both `context.close()` and
    `browser.close()` were still reached in the finally teardown (acceptance #6 / L5).
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.adapters import greenhouse as gh_module
    from src.browser.session import session

    reached = {"context_close": False, "browser_close": False}

    # Monkeypatched adapter.apply: first call proceeds; second call raises.
    real_apply = gh_module.GreenhouseAdapter.apply
    call_count = {"n": 0}

    def boom(self, page, ctx):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated mid-fill failure to exercise L5 teardown")
        return real_apply(self, page, ctx)

    monkeypatch.setattr(gh_module.GreenhouseAdapter, "apply", boom)

    exc_seen = None
    try:
        with session(transport="local") as (browser, context, page):
            # Wrap close() on both to record they were invoked during teardown.
            orig_ctx_close = context.close
            orig_br_close = browser.close

            def track_ctx_close(*a, **kw):
                reached["context_close"] = True
                return orig_ctx_close(*a, **kw)

            def track_br_close(*a, **kw):
                reached["browser_close"] = True
                return orig_br_close(*a, **kw)

            context.close = track_ctx_close  # type: ignore[assignment]
            browser.close = track_br_close  # type: ignore[assignment]

            adapter = gh_module.GreenhouseAdapter()
            adapter.apply(page, sample_apply_context)  # succeeds
            adapter.apply(page, sample_apply_context)  # raises inside the with-block
    except RuntimeError as e:
        exc_seen = e

    assert exc_seen is not None, (
        "monkeypatched adapter did not raise — L5 teardown check is meaningless."
    )
    assert reached["context_close"], (
        "L5 violation: context.close() not reached in try/finally teardown."
    )
    assert reached["browser_close"], (
        "L5 violation: browser.close() not reached in try/finally teardown."
    )


# ========================================================================
# Test 6 — first log event is apply.live.starting; no PII anywhere.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_starting_log_event_no_pii(
    require_live_env,
    apply_settings,
    sample_apply_context,
    capture_logs,
    sample_candidate_profile,
):
    """First captured log event is `apply.live.starting` with board='greenhouse',
    mode='review', dry_run=True (acceptance #14). No candidate PII anywhere in
    captured events (acceptance #13, landmine L7).
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.browser.session import session

    with session(transport="local") as (browser, context, page):
        GreenhouseAdapter().apply(page, sample_apply_context)

    events = capture_logs.events
    assert events, "expected at least one captured log event"
    first = events[0]
    assert first.get("event") == STARTING_LOG_EVENT, (
        f"first log event must be {STARTING_LOG_EVENT!r}, got {first.get('event')!r}"
    )
    assert first.get("board") == "greenhouse", (
        f"first log event board must be 'greenhouse', got {first.get('board')!r}"
    )
    assert first.get("mode") == "review", (
        f"first log event mode must be 'review', got {first.get('mode')!r}"
    )
    assert first.get("dry_run") is True, (
        f"first log event dry_run must be True, got {first.get('dry_run')!r}"
    )
    capture_logs.assert_no_pii(sample_candidate_profile)


# ========================================================================
# Test 7 — L1: confirmation marker does NOT match pre-submit DOM.
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_confirmation_marker_never_matches_pre_submit_text_L1(
    require_live_env,
    apply_settings,
    sample_apply_context,
):
    """After form fill but BEFORE submit (dry_run halts here), the pre-submit
    page must NOT contain any `[class*='application-confirmation']` node.
    Validates that L1 discipline holds against a real Greenhouse-served DOM,
    not just a synthetic fixture.
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.browser.session import session

    with session(transport="local") as (browser, context, page):
        result = GreenhouseAdapter().apply(page, sample_apply_context)
        # L10: check presence explicitly (query_selector may return None on a real DOM).
        pre_submit_confirm = page.query_selector('[class*="application-confirmation"]')
        assert pre_submit_confirm is None, (
            "L1 violation: pre-submit DOM already exposes an application-confirmation "
            "node; a regex-based text-match confirmation detector would false-positive here."
        )

    assert result.status == "review_required", (
        f"dry-run must halt at pre-submit — expected review_required, got {result.status!r}"
    )


# ========================================================================
# Test 8 — L4: select_option uses label= kwarg (never positional value).
# ========================================================================
@pytest.mark.live_ats
def test_greenhouse_demo_select_by_label_L4(
    require_live_env,
    apply_settings,
    sample_apply_context,
    monkeypatch,
):
    """Wrap page.select_option to record kwargs; assert at least one call used
    `label=` (never positional 'Yes'/'No' value). Greenhouse renders <select>
    options with numeric `value=` IDs so matching by positional value is L4.
    """
    _assert_dry_run_first(apply_settings)
    _assert_target_guard()

    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.browser.session import session

    recorded = []

    with session(transport="local") as (browser, context, page):
        original_select = page.select_option

        def recording_select(selector, *args, **kwargs):
            recorded.append(
                {"selector": selector, "args": list(args), "kwargs": dict(kwargs)}
            )
            return original_select(selector, *args, **kwargs)

        monkeypatch.setattr(page, "select_option", recording_select)
        GreenhouseAdapter().apply(page, sample_apply_context)

    label_calls = [c for c in recorded if "label" in c["kwargs"]]
    assert label_calls, (
        f"L4 violation: no page.select_option() call used the label= kwarg. "
        f"Recorded calls: {recorded!r}"
    )
    for call in label_calls:
        # A label= call must NOT also pass a positional 'Yes'/'No' value.
        assert not any(
            isinstance(a, str) and a in ("Yes", "No") for a in call["args"]
        ), (
            f"L4 violation: select_option mixed label= kwarg with a positional "
            f"'Yes'/'No' value — {call!r}"
        )


# ========================================================================
# Test 9 — pyproject.toml registers the live_ats marker + addopts.
# ========================================================================
@pytest.mark.live_ats
def test_pyproject_registers_live_ats_marker():
    """Read pyproject.toml (or pytest.ini fallback) and assert the live_ats marker
    is registered under [tool.pytest.ini_options].markers AND that addopts
    excludes it by default (acceptance #1). Runs offline; decorated so it stays
    inside the opt-in invocation and does not add noise to default runs.
    """
    repo_root = Path(__file__).resolve().parents[3]
    pyproject = repo_root / "pyproject.toml"
    pytest_ini = repo_root / "pytest.ini"

    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        assert "[tool.pytest.ini_options]" in text, (
            "pyproject.toml missing [tool.pytest.ini_options] block (spec §Interfaces)."
        )
        assert "live_ats:" in text, (
            "pyproject.toml [tool.pytest.ini_options].markers must register "
            "'live_ats:' (acceptance #1)."
        )
        assert "not live_ats" in text, (
            "pyproject.toml [tool.pytest.ini_options].addopts must include "
            "\"-m 'not live_ats'\" (acceptance #1)."
        )
    elif pytest_ini.exists():
        text = pytest_ini.read_text(encoding="utf-8")
        assert "live_ats:" in text, "pytest.ini must register live_ats marker."
        assert "not live_ats" in text, "pytest.ini addopts must exclude live_ats by default."
    else:
        pytest.fail(
            "neither pyproject.toml nor pytest.ini found — live_ats marker unregistered."
        )
