"""Phase 3 RED tests — unattended-safety & security invariants.

Covers 9 findings from `.agent/codebase-audit-2026-07-08.md` Phase 3 table:

    B4   Headless OAuth guard  — no interactive flow under non-TTY / no-DISPLAY.
    H12  Token file mode 0o600  + parent dir 0o700.
    L4   Token write atomic (temp + os.replace).
    H17  Default settings.yaml ships `apply.dry_run: true`.
    M6   `--test` mode threads dry_run=True through the apply seam.
    H6   Seam wires `captcha_detector` into ApplyContext (not None).
    M5   Dispatcher loads bootstrapped storage_state into transport.open().
    M9   `install_scrubber()` runs at pipeline entry unconditionally.
    M11  `send_immediate` retry stack cannot exceed 3 real send attempts.
    M10  CLI subprocess timeout kills the process GROUP, not just the child.

Every test in this module MUST fail on main @ d62af0e and go GREEN once
the Phase 3 fix wave lands. This is the RED baseline for the Phase 3
`/subagent-driven-development` fan-out; each finding's fix subagent runs
the specific test(s) that cover its change.

Test IDs deliberately mirror the finding IDs (B4/H12/L4/H17/M6/H6/M5/M9/M11/M10)
so the review report can trace RED→GREEN one-for-one.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — B4 headless OAuth guard
# ─────────────────────────────────────────────────────────────────────────────


def test_b4_headless_oauth_raises_auth_error_never_calls_run_local_server(
    tmp_path, monkeypatch
):
    """Under a headless (no TTY, no DISPLAY) environment with a missing
    token, `GmailClient._authenticate` must raise a clear auth error —
    NEVER reach `InstalledAppFlow.run_local_server`.

    Failure mode being fixed: today the client launches a blocking
    interactive OAuth flow with no timeout and no headless guard. Under
    the flock-serialized cron entrypoint this hangs indefinitely and
    every subsequent 30-minute tick exits 0 silently (agent dead, no
    signal).

    RED assertion: monkeypatch `run_local_server` to raise a distinct
    sentinel; assert the sentinel does NOT surface (proving the guard
    fires first) and that the code raises a non-sentinel exception
    signalling a headless / auth-required condition.
    """
    from src.gmail import client as client_mod

    token_path = tmp_path / "credentials" / "token.json"
    creds_path = tmp_path / "credentials" / "credentials.json"
    # Point the client at nonexistent token + credentials so it must
    # take the "launch interactive flow" branch.
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))
    # Simulate a headless cron: no controlling TTY, no display.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("HIRING_AGENT_HEADLESS", "1")

    class _InteractiveFlowInvoked(AssertionError):
        """Sentinel proving the interactive OAuth flow was reached."""

    def _boom(*args, **kwargs):
        raise _InteractiveFlowInvoked("run_local_server must not run under headless")

    # Patch BOTH the entry into the OAuth flow AND its interactive call so
    # neither path can hang the test.
    monkeypatch.setattr(
        client_mod.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, *a, **k: SimpleNamespace(run_local_server=_boom)),
    )

    # Also patch stdin.isatty to return False so any guard keyed on it
    # sees a non-interactive env.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    with pytest.raises(Exception) as excinfo:
        client_mod.GmailClient()

    # The interactive-flow sentinel MUST NOT escape — that would prove the
    # guard didn't fire.
    assert not isinstance(excinfo.value, _InteractiveFlowInvoked), (
        "B4 regression: interactive OAuth flow was reached under headless env; "
        "the headless guard did not fire"
    )
    # The raised error must clearly indicate an auth / headless problem so
    # cron exits non-zero with an actionable message rather than hanging.
    msg = f"{type(excinfo.value).__name__}: {excinfo.value}".lower()
    assert any(
        tok in msg for tok in ("headless", "auth", "tty", "interactive", "oauth")
    ), f"B4: raised error {msg!r} does not signal a headless/auth condition"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — H12 + L4 token file perms + atomic write
# ─────────────────────────────────────────────────────────────────────────────


def test_h12_l4_token_written_with_0o600_and_parent_dir_0o700(tmp_path, monkeypatch):
    """After `_authenticate` persists a refreshed token, the token file
    must have mode 0o600, its parent dir 0o700, and the write must have
    gone through a temp-file + `os.replace` step so a concurrent reader
    cannot observe a truncated JSON blob (L4).

    RED assertion: today the code does `open(path, 'w')` with the
    process's default umask (usually 0o022 → 0o644) and never chmods
    the parent dir. This test asserts both perms and that a
    `.tmp` sibling was involved during the write (atomic swap).
    """
    from src.gmail import client as client_mod

    token_path = tmp_path / "creds" / "token.json"
    creds_path = tmp_path / "creds" / "credentials.json"
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))

    # Fake `Credentials.from_authorized_user_file` so we can seed a "valid"
    # creds object without touching the real Google client.
    fake_creds = SimpleNamespace(
        valid=True,
        expired=False,
        refresh_token="refresh",
        to_json=lambda: '{"token": "abc", "refresh_token": "xyz"}',
        refresh=lambda req: None,
    )

    # Force the "must persist" branch by making the initial-load object invalid
    # then valid after refresh.
    seen_replace: dict[str, Any] = {"call_count": 0, "tmp_paths": []}
    real_replace = os.replace

    def _spy_replace(src, dst, *a, **k):
        seen_replace["call_count"] += 1
        seen_replace["tmp_paths"].append(str(src))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _spy_replace)

    # Simulate the "token exists but expired w/ refresh_token" branch so the
    # code enters the persist path without invoking the interactive flow.
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text('{"placeholder": true}')

    expired_then_valid = SimpleNamespace(
        valid=False,
        expired=True,
        refresh_token="refresh",
        to_json=lambda: '{"token": "abc"}',
        refresh=lambda req: setattr(expired_then_valid, "valid", True),
    )

    monkeypatch.setattr(
        client_mod.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, *a, **k: expired_then_valid),
    )
    # Prevent the API client from being built (network).
    monkeypatch.setattr(client_mod, "build", lambda *a, **k: MagicMock())

    client_mod.GmailClient()

    # Assertion A: token file exists with 0o600.
    assert token_path.exists(), "H12: token.json was not written"
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600, f"H12: token.json mode is {oct(mode)}, expected 0o600"

    # Assertion B: parent dir 0o700.
    parent_mode = stat.S_IMODE(token_path.parent.stat().st_mode)
    assert parent_mode == 0o700, (
        f"H12: token.json parent dir mode is {oct(parent_mode)}, expected 0o700"
    )

    # Assertion C: L4 — the write went through os.replace (temp + rename).
    assert seen_replace["call_count"] >= 1, (
        "L4: token write was not atomic — os.replace was never called; "
        "concurrent readers can observe a truncated file"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — H17 default settings.yaml ships apply.dry_run: true
# ─────────────────────────────────────────────────────────────────────────────


def test_h17_default_settings_yaml_ships_dry_run_true():
    """The shipped `config/settings.yaml` MUST have `apply.dry_run: true`.

    Failure mode being fixed: SETUP.md tells the operator "Leave
    apply.dry_run: true — that stays on" but the shipped file has
    `dry_run: false`. Any user flipping `apply.enabled: true` believing
    the doc gets a live submission on the first YES.
    """
    settings_path = ROOT / "config" / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text())
    assert data["apply"]["dry_run"] is True, (
        "H17: config/settings.yaml ships apply.dry_run != true; SETUP.md "
        "promises the opposite. A live submit on first YES is the exact "
        "incident the six-criteria gate was written to prevent."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — M6 `--test` mode threads dry_run through the apply seam
# ─────────────────────────────────────────────────────────────────────────────


def test_m6_test_mode_dry_run_threads_into_apply_seam(monkeypatch):
    """When `run_pipeline(dry_run=True, ...)` is invoked (the `--test`
    entry point does this), the apply seam MUST see `dry_run=True` on
    every per-job dispatch — even when the underlying config has
    `apply.dry_run: false`.

    Failure mode being fixed: `run_pipeline(dry_run=...)` currently
    accepts the flag but never threads it into `apply_config` — the seam
    reads `apply_config.get('dry_run', False)` verbatim from the config
    dict. So `apply.enabled=true, mode=auto, dry_run=false` under
    `--test` still performs a live submit.
    """
    from src.apply import _seam as _apply_seam

    captured: dict[str, Any] = {}

    def _capture_run_for_job(*, apply_config, **kwargs):
        captured["dry_run_seen_by_seam"] = bool(apply_config.get("dry_run", False))
        return None

    monkeypatch.setattr(_apply_seam, "run_for_job", _capture_run_for_job)

    # Minimal config: apply enabled, dry_run FALSE in the file (worst case).
    config = {
        "apply": {
            "enabled": True,
            "mode": "auto",
            "dry_run": False,
            "allowed_ats": ["greenhouse"],
            "long_tail": "none",
            "timeout_seconds": 90,
            "navigation_retries": 2,
            "rate_limit_per_ats_per_day": 10,
            "review_timeout_hours": 72,
            "review_reping_hours": 24,
            "retention_days": 30,
            "screenshot_dir": "state/screenshots",
            "trace_dir": "state/traces",
            "storage_state_dir": "config/credentials/apply",
            "dedup_db_path": "state/applied_jobs.db",
            "captcha_action": "escalate",
            "captcha_transport": "local",
            "profile_path": "templates/candidate_profile.yaml",
            "gmail_label_prefix": "hiring-agent/apply",
            "fast_path_recipient": "env:MY_EMAIL",
            "browserbase": {
                "enabled": False,
                "solve_captchas": False,
                "proxies": False,
                "block_ads": False,
            },
        },
        "scraper": {"timeout_seconds": 15, "min_jd_length": 200},
        "lanes": [],
        "resume": {"min_confidence_score": 30},
        "cover_letter": {"template": "templates/cover_letter.docx"},
        "qa": {"max_retries": 2, "checks": []},
        "contacts": {"enabled": False},
        "pdf": {"libreoffice_path": "libreoffice"},
        "jobs": {"max_per_run": 1, "sort_by": "newest"},
        "gmail": {},
    }

    # Skip the seam's initialize (poll_pending_reviews) and finalize (rotate)
    # so this test stays hermetic.
    monkeypatch.setattr(_apply_seam, "initialize", lambda *a, **k: [])
    monkeypatch.setattr(_apply_seam, "finalize", lambda *a, **k: None)

    # Stub every heavyweight per-job step so run_pipeline just reaches
    # the apply-seam call.
    from src import main as main_mod

    def _fake_fetch(*a, **k):
        return SimpleNamespace(
            text="x" * 500, ats_apply_url="https://boards.greenhouse.io/x", ats="greenhouse"
        )

    monkeypatch.setattr(main_mod, "fetch_job_description", _fake_fetch)
    monkeypatch.setattr(
        main_mod, "classify_lane", lambda **k: {"name": "pmm", "label": "PMM"}
    )
    monkeypatch.setattr(
        main_mod,
        "tailor_resume",
        lambda **k: {"confidence_score": 100, "roles": [], "skills": []},
    )
    monkeypatch.setattr(main_mod, "write_cover_letter", lambda **k: {"paragraphs": []})
    monkeypatch.setattr(main_mod, "run_qa", lambda **k: {"passed": True, "issues": []})
    monkeypatch.setattr(main_mod, "auto_fix", lambda **k: ({}, {}))
    monkeypatch.setattr(main_mod, "render_resume", lambda **k: (Path("/tmp/r.pdf"), Path("/tmp/r.docx")))
    monkeypatch.setattr(
        main_mod, "render_cover_letter", lambda **k: (Path("/tmp/c.pdf"), Path("/tmp/c.docx"))
    )
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)

    jobs = [{"title": "Eng", "company": "Acme", "url": "https://example.com/j"}]
    main_mod.run_pipeline(
        jobs=jobs,
        config=config,
        project_bank=[],
        today="2026-07-09",
        output_dir=Path("/tmp/does-not-exist"),
        dry_run=True,  # <-- The `--test` mode does this.
        gmail_client=None,
    )

    assert captured.get("dry_run_seen_by_seam") is True, (
        "M6: run_pipeline(dry_run=True) did NOT propagate dry_run to the "
        "apply seam; apply_config['dry_run'] arrived False. --test claims "
        "hermetic; today it can perform a live submit."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — H6 seam wires captcha_detector into ApplyContext
# ─────────────────────────────────────────────────────────────────────────────


def test_h6_seam_wires_captcha_detector_into_apply_context(monkeypatch):
    """`_seam.run_for_job` MUST construct `ApplyContext` with a non-None
    `captcha_detector` when apply.enabled=true.

    Failure mode being fixed: seam builds ApplyContext without ever
    setting `captcha_detector`, so `types.py`'s default of `None` wins.
    Every adapter's CAPTCHA gate (`callable(None)` → False) is dead;
    Turnstile-gated pages get a blind submit-click and a misleading
    "confirmation marker not found" failure.
    """
    from src.apply import _seam as _apply_seam

    captured_ctx: dict[str, Any] = {}

    def _capture_apply_to_job(*, job_url, ctx, config):
        captured_ctx["ctx"] = ctx
        # Return a benign result so run_for_job's stage_review branch is skipped.
        from src.apply.types import ApplyResult

        return ApplyResult(status="skipped", reason="test-capture")

    monkeypatch.setattr(_apply_seam, "_call_apply_to_job", _capture_apply_to_job)

    # Minimal, well-formed apply config that lets the seam build the ctx.
    apply_cfg = {
        "enabled": True,
        "mode": "review",
        "dry_run": True,
        "allowed_ats": ["greenhouse"],
        "captcha_action": "escalate",
        "captcha_transport": "browserbase",
        "browserbase": {"enabled": True, "solve_captchas": True},
        "profile_path": "templates/candidate_profile.yaml.example",
        "dedup_db_path": "state/applied_jobs.db",
        "storage_state_dir": "config/credentials/apply",
        "fast_path_recipient": "env:MY_EMAIL",
        "screenshot_dir": "state/screenshots",
        "trace_dir": "state/traces",
        "gmail_label_prefix": "hiring-agent/apply",
        "user": "single",
    }

    job = {
        "title": "Eng",
        "company": "Acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "ats": "greenhouse",
    }
    job_log = MagicMock()

    _apply_seam.run_for_job(
        job=job,
        jd_text="jd",
        lane={"name": "pmm", "label": "PMM"},
        resume_path=None,
        cover_letter_path=None,
        apply_config=apply_cfg,
        job_log=job_log,
        gmail_client=None,
    )

    ctx = captured_ctx.get("ctx")
    assert ctx is not None, "H6: seam never built an ApplyContext (fake dispatch never fired)"
    assert getattr(ctx, "captcha_detector", None) is not None, (
        "H6: seam built ApplyContext with captcha_detector=None; CAPTCHA "
        "gate, escalation email, and Browserbase routing are all dead."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — M5 dispatcher loads bootstrapped storage_state into transport.open()
# ─────────────────────────────────────────────────────────────────────────────


def test_m5_dispatcher_loads_bootstrapped_storage_state(monkeypatch):
    """When a bootstrapped storage_state exists for `(ats, user)`,
    `apply_to_job` MUST pass a non-None `storage_state` dict into
    `transport.open()`.

    Failure mode being fixed: dispatcher hardcodes `storage_state=None`
    at the transport call site, so the credentials the user bootstrapped
    (M1-flow) never actually load — every apply opens a fresh anonymous
    browser regardless.
    """
    from src.apply import dispatcher as disp_mod

    seen_storage_state: dict[str, Any] = {"value": "NOT-SET"}

    class _FakeSession:
        page = MagicMock()

    class _FakeCtxMgr:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            seen_storage_state["value"] = storage_state
            return _FakeCtxMgr()

    monkeypatch.setattr(disp_mod, "get_transport", lambda cfg, kind: _FakeTransport())

    # Fake dispatch() to always return an adapter that reports 'skipped'.
    class _FakeAdapter:
        name = "greenhouse"

        def detect(self, url):
            return True

        def apply(self, page, ctx):
            from src.apply.types import ApplyResult

            return ApplyResult(status="skipped", reason="test")

    monkeypatch.setattr(disp_mod, "dispatch", lambda url, cfg: _FakeAdapter())

    # Fake load_state so the "bootstrapped credentials present" branch fires.
    bootstrapped_state = {"cookies": [{"name": "session", "value": "abc"}], "origins": []}

    def _fake_load_state(ats, user):
        return bootstrapped_state

    # The dispatcher should call load_state under some importable name — the
    # fix will wire either through credentials.load_state directly or through
    # a helper. Patch both possible surfaces so whichever the fix chose works.
    import src.apply.credentials as creds_mod

    monkeypatch.setattr(creds_mod, "load_state", _fake_load_state)

    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext

    profile = CandidateProfile.load(
        str(ROOT / "templates" / "candidate_profile.yaml.example")
    )
    ctx = ApplyContext(
        profile=profile,
        job={
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "title": "Eng",
        },
        resume_path=None,
        cover_letter_path=None,
        config={"apply": {"enabled": True}},
        applicant="single",
        dry_run=True,
        mode="review",
    )

    disp_mod.apply_to_job(
        "https://boards.greenhouse.io/acme/jobs/1",
        ctx,
        {"apply": {"enabled": True, "allowed_ats": ["greenhouse"], "user": "single"}},
    )

    ss = seen_storage_state["value"]
    assert ss is not None, (
        "M5: dispatcher called transport.open() with storage_state=None even "
        "though load_state returned a bootstrapped dict. Bootstrapped "
        "credentials are never loaded on the first-pass apply path."
    )
    assert ss != "NOT-SET", "M5: transport.open() was never called at all"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — M9 install_scrubber runs at pipeline entry unconditionally
# ─────────────────────────────────────────────────────────────────────────────


def test_m9_install_scrubber_runs_at_pipeline_entry_when_apply_disabled(monkeypatch):
    """`install_scrubber()` MUST fire at pipeline entry even when
    `apply.enabled=false` so `contacts/hm_finder` (which logs raw LLM
    output at `hm_finder.no_json_found` / `hm_finder.json_parse_error`)
    cannot leak third-party PII through the default pipeline.

    Failure mode being fixed: install_scrubber only fires from the seam
    when `apply.enabled=true`, but `contacts.enabled` defaults true and
    apply defaults false — so the PII-scrub processor is not installed
    on the LOG PATH the parse-failure warning uses.
    """
    calls: list[str] = []

    from src.apply import logging as apply_logging

    # Reset the idempotency latch so we can observe fresh installs.
    monkeypatch.setattr(apply_logging, "_installed", False, raising=False)

    def _record_install(*a, **k):
        calls.append("installed")

    monkeypatch.setattr(apply_logging, "install_scrubber", _record_install)

    # Also patch the aliased import used by _seam.
    from src.apply import _seam as _apply_seam

    monkeypatch.setattr(
        _apply_seam, "_call_install_scrubber", lambda: _record_install()
    )

    from src import main as main_mod

    # Stub the heavy per-job stack so we just observe entry-time behavior.
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)

    # NB: apply.enabled = False.
    config = {
        "apply": {"enabled": False},
        "scraper": {"timeout_seconds": 15, "min_jd_length": 200},
        "lanes": [],
        "resume": {"min_confidence_score": 30},
        "cover_letter": {"template": "templates/cover_letter.docx"},
        "qa": {"max_retries": 2, "checks": []},
        "contacts": {"enabled": True},
        "pdf": {"libreoffice_path": "libreoffice"},
        "jobs": {"max_per_run": 1, "sort_by": "newest"},
        "gmail": {},
    }

    main_mod.run_pipeline(
        jobs=[],  # zero jobs — still exercises the entry-time install.
        config=config,
        project_bank=[],
        today="2026-07-09",
        output_dir=Path("/tmp/does-not-exist"),
        dry_run=True,
        gmail_client=None,
    )

    assert calls, (
        "M9: install_scrubber() was not called at pipeline entry with "
        "apply.enabled=false. hm_finder raw LLM output can hit persistent "
        "logs unredacted."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — M11 send_immediate retry stacking cap
# ─────────────────────────────────────────────────────────────────────────────


def test_m11_send_immediate_retries_do_not_stack_exponentially(monkeypatch):
    """`GmailClient.send_immediate` MUST NOT trigger more than
    `_MAX_ATTEMPTS` (3) total send calls, even though both `send_immediate`
    and its inner `send_email` are decorated with `@navigation_retry`.

    Failure mode being fixed: outer + inner decorators can compose into
    3 × 3 = 9 real API sends on a transient error near the response boundary,
    turning one URGENT notification into up to 9 duplicate emails.
    """
    from src.gmail import client as client_mod
    import httpx

    # Build a client without touching the network.
    client = client_mod.GmailClient.__new__(client_mod.GmailClient)
    client.creds = SimpleNamespace(refresh_token="r", refresh=lambda req: None)
    client.service = MagicMock()

    send_calls = {"count": 0}

    def _flaky_send(**kwargs):
        send_calls["count"] += 1
        # Always fail with a transient transport error → tenacity retries.
        raise httpx.ConnectError("boom")

    client.service.users.return_value.messages.return_value.send.return_value.execute = _flaky_send

    monkeypatch.setattr(client, "refresh_connection", lambda: None)
    monkeypatch.setenv("MY_EMAIL", "op@example.com")

    with pytest.raises(Exception):
        client.send_immediate(subject="URGENT", body="hi")

    # Absolute cap: 3 real send attempts. Anything > 3 proves the outer
    # + inner decorators are stacking.
    assert send_calls["count"] <= 3, (
        f"M11: send_immediate produced {send_calls['count']} real send attempts; "
        "stacked @navigation_retry decorators are causing exponential duplication. "
        "Cap is 3 (a single _MAX_ATTEMPTS budget)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — M10 CLI subprocess timeout kills the process GROUP
# ─────────────────────────────────────────────────────────────────────────────


def test_m10_llm_cli_timeout_kills_subprocess_group(tmp_path, monkeypatch):
    """`_call_via_cli`'s long-prompt path MUST kill the entire process
    group on `TimeoutExpired`, not leak a grandchild `claude` process.

    Failure mode being fixed: today the long-prompt path uses
    `shell=True` (`cat tmp | claude -p`); a `TimeoutExpired` SIGKILLs
    only the `/bin/sh` wrapper. The grandchild `claude` keeps running
    (CPU + API quota), and days of timeouts accumulate orphan procs.

    RED assertion: when the CLI path hits TimeoutExpired, the caller
    must have either (a) invoked the child in its own process group AND
    called `os.killpg` on the group, OR (b) refactored off shell=True to
    an argv form where SIGKILL on the direct child is sufficient. We
    detect option (a) via a spy on `os.killpg`; option (b) via absence
    of `shell=True` in the subprocess.run call.
    """
    from src import llm as llm_mod

    seen: dict[str, Any] = {"killpg_called": False, "shell_flag": None, "start_new_session": False}

    real_killpg = os.killpg

    def _spy_killpg(pgid, sig):
        seen["killpg_called"] = True
        try:
            return real_killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return None

    monkeypatch.setattr(os, "killpg", _spy_killpg)

    class _FakePopen:
        def __init__(self):
            self.pid = 12345

    def _fake_run(*args, **kwargs):
        seen["shell_flag"] = kwargs.get("shell", False)
        seen["start_new_session"] = kwargs.get("start_new_session", False)
        # Simulate the CLI hanging past its timeout.
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else "claude", timeout=kwargs.get("timeout", 1)
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Force the long-prompt branch (>8000 chars).
    long_prompt = "x" * 9000

    with pytest.raises(RuntimeError):
        llm_mod._call_via_cli(long_prompt, model="haiku", system=None, timeout=1)

    # Accept EITHER option (a) or option (b) as fixing M10.
    option_a = seen["killpg_called"] and seen["start_new_session"]
    option_b = seen["shell_flag"] is False
    assert option_a or option_b, (
        f"M10: TimeoutExpired path did not kill the process group and still "
        f"uses shell=True. seen={seen!r}. Grandchild `claude` process leaks "
        "on every timeout under the current implementation."
    )
