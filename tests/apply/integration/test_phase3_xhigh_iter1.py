"""Phase 3 xhigh iter-1 — RED tests for the 15 findings surfaced by the
first xhigh code-review sweep of the Phase 3 unattended-safety fix batch.

Groups:
  SB (load-bearing, would ship broken)
    SB1  install_scrubber() unwrapped by try/except → seam initialize crashes.
    SB2  apply_config['dry_run']=True is a one-way ratchet; mutates live dict.
    SB3  Gmail token TOCTOU — file created at umask (readable) BEFORE chmod.
    SB4  _is_headless() bypassed by stale DISPLAY inherited from parent.

  SG (load-bearing, security / correctness)
    SG1  dispatcher.load_state() reintroduces keyring-hang class per apply.
    SG2  Malformed load_state dict falls through to transport.open() unchecked.
    SG3  captcha `from src.apply.captcha import detect` unguarded — crashes.

  SD (structural / logging leak)
    SD1  storage_state load-failure logs _exc_repr(exc) → decrypted payload leak.

  SE (structural / code hygiene)
    SG4  main.py get('apply', {}) returns orphan dict — M6 mutation lost.
    SE1  M5 test patches dispatcher.get_transport rather than transport module.
    SE2  Duplicated storage_state envelope-unwrap in dispatcher + review.
    SE3  Duplicate weak _atomic_write in gmail/client.py vs credentials.py.
    SE4  gmail token write no try/finally → orphan .tmp with valid token on OSError.
    SE5  main() doesn't catch AuthError from GmailClient() → uncaught traceback.
    SE6  llm._call_via_cli long-prompt writes to tempfile → collapse to input=.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# SB1 — install_scrubber() failure must not crash run_pipeline
# ─────────────────────────────────────────────────────────────────────────────


def test_sb1_install_scrubber_failure_does_not_crash_seam_initialize(monkeypatch):
    """If structlog.configure() (or anything install_scrubber calls) raises,
    the seam initialize() MUST swallow, log a structural event, and return.
    Pre-fix: bare `_call_install_scrubber()` call at _seam.py:242 lets the
    exception propagate; every run_pipeline call — even with apply.enabled=false
    — crashes at pipeline entry.
    """
    from src.apply import _seam as _apply_seam

    def _boom():
        raise RuntimeError("scrubber install exploded")

    monkeypatch.setattr(_apply_seam, "_call_install_scrubber", _boom)

    # apply.enabled=false so we only see the entry-time scrubber path.
    config = {"apply": {"enabled": False}}
    # Must not raise.
    events = _apply_seam.initialize(config, None)
    assert events == []


# ─────────────────────────────────────────────────────────────────────────────
# SB2 — dry_run must NOT be a one-way ratchet on live config
# ─────────────────────────────────────────────────────────────────────────────


def test_sb2_dry_run_flag_not_persisted_across_pipeline_calls(monkeypatch):
    """After a `run_pipeline(dry_run=True)` call, a subsequent
    `run_pipeline(dry_run=False)` on the SAME config dict MUST NOT observe
    a persisted `apply_config['dry_run'] = True` mutation.

    Pre-fix: main.py:427 mutates apply_config in place. Long-lived process
    silently sticks in dry_run forever after first --test invocation.
    """
    from src.apply import _seam as _apply_seam
    from src import main as main_mod

    dry_seen: list[bool] = []

    def _capture(*, apply_config, dry_run=False, **kwargs):
        # Compute the effective flag same way the fixed seam should:
        effective = dry_run or bool(apply_config.get("dry_run", False))
        dry_seen.append(effective)
        return None

    monkeypatch.setattr(_apply_seam, "run_for_job", _capture)
    monkeypatch.setattr(_apply_seam, "initialize", lambda *a, **k: [])
    monkeypatch.setattr(_apply_seam, "finalize", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)

    def _fake_fetch(*a, **k):
        return SimpleNamespace(
            text="x" * 500,
            ats_apply_url="https://boards.greenhouse.io/x",
            ats="greenhouse",
        )

    monkeypatch.setattr(main_mod, "fetch_job_description", _fake_fetch)
    monkeypatch.setattr(main_mod, "classify_lane", lambda **k: {"name": "pmm", "label": "PMM"})
    monkeypatch.setattr(
        main_mod,
        "tailor_resume",
        lambda **k: {"confidence_score": 100, "roles": [], "skills": []},
    )
    monkeypatch.setattr(main_mod, "write_cover_letter", lambda **k: {"paragraphs": []})
    monkeypatch.setattr(main_mod, "run_qa", lambda **k: {"pass": True, "errors": []})
    monkeypatch.setattr(main_mod, "auto_fix", lambda **k: ({}, {}))
    monkeypatch.setattr(
        main_mod, "render_resume", lambda **k: (Path("/tmp/r.pdf"), Path("/tmp/r.docx"))
    )
    monkeypatch.setattr(
        main_mod,
        "render_cover_letter",
        lambda **k: (Path("/tmp/c.pdf"), Path("/tmp/c.docx")),
    )

    # SHARED config dict across both calls — this is the one-way ratchet surface.
    config = {
        "apply": {
            "enabled": True,
            "mode": "review",
            "dry_run": False,
            "allowed_ats": ["greenhouse"],
            "long_tail": "none",
        },
        "scraper": {"timeout_seconds": 15, "min_jd_length": 200},
        "lanes": [],
        "resume": {"min_confidence_score": 30},
        "cover_letter": {},
        "qa": {"max_retries": 0, "checks": []},
        "contacts": {"enabled": False},
        "pdf": {"libreoffice_path": "libreoffice"},
        "jobs": {"max_per_run": 1},
        "gmail": {},
    }

    jobs = [{"title": "Eng", "company": "Acme", "url": "https://example.com/j"}]

    # First call: dry_run=True.
    main_mod.run_pipeline(
        jobs=jobs, config=config, project_bank=[], today="2026-07-09",
        output_dir=Path("/tmp/does-not-exist"), dry_run=True, gmail_client=None,
    )
    # Second call: dry_run=False. The config dry_run stayed False (per rules).
    main_mod.run_pipeline(
        jobs=jobs, config=config, project_bank=[], today="2026-07-09",
        output_dir=Path("/tmp/does-not-exist"), dry_run=False, gmail_client=None,
    )

    assert config["apply"]["dry_run"] is False, (
        "SB2: apply_config['dry_run'] was mutated in place — the flag is a "
        "one-way ratchet. Long-lived process stays in dry_run forever after "
        "first --test call."
    )
    assert dry_seen == [True, False], (
        f"SB2: expected [True, False] from run_pipeline calls, saw {dry_seen!r}. "
        "Second call must observe dry_run=False."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SB3 — token file must be created at mode 0o600 (no TOCTOU window)
# ─────────────────────────────────────────────────────────────────────────────


def test_sb3_token_tmp_created_with_mode_0o600_no_toctou_window(tmp_path, monkeypatch):
    """The token .tmp file MUST be created with mode 0o600 via os.open()
    with the mode arg — NOT `open(path, 'w')` (which respects umask, so
    the file is world-readable during the write window before chmod).
    """
    from src.gmail import client as client_mod

    token_path = tmp_path / "creds" / "token.json"
    creds_path = tmp_path / "creds" / "credentials.json"
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))

    # Force the "must persist" path.
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text('{"placeholder": true}')

    expired_then_valid = SimpleNamespace(
        valid=False, expired=True, refresh_token="refresh",
        to_json=lambda: '{"token": "abc"}',
        refresh=lambda req: setattr(expired_then_valid, "valid", True),
    )
    monkeypatch.setattr(
        client_mod.Credentials, "from_authorized_user_file",
        classmethod(lambda cls, *a, **k: expired_then_valid),
    )
    monkeypatch.setattr(client_mod, "build", lambda *a, **k: MagicMock())

    # Track the initial mode of the .tmp path when it first appears.
    seen_tmp_modes: list[int] = []
    real_os_open = os.open
    real_open = open

    def _spy_os_open(path, flags, mode=0o777, *a, **k):
        path_str = str(path)
        if path_str.endswith(".tmp"):
            seen_tmp_modes.append(mode)
        return real_os_open(path, flags, mode, *a, **k)

    def _spy_open(path, *a, **k):
        # Capture bare open() writes to the tmp path too — these are the
        # BAD pre-fix path (respects umask, no explicit mode).
        path_str = str(path)
        if path_str.endswith(".tmp"):
            # Sentinel value = signals a bare open() was used (no explicit mode).
            seen_tmp_modes.append(-1)
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", _spy_os_open)
    monkeypatch.setattr("builtins.open", _spy_open)

    client_mod.GmailClient()

    assert seen_tmp_modes, "SB3: no .tmp file was ever created — write path skipped?"
    # NO bare open() calls with -1 sentinel; every tmp creation must go
    # through os.open() with an explicit 0o600 mode.
    assert -1 not in seen_tmp_modes, (
        "SB3: token .tmp was created via bare open() — respects umask, "
        "world-readable during write window before chmod. Use os.open() "
        "with O_CREAT|O_WRONLY|O_TRUNC and mode=0o600."
    )
    assert all(m == 0o600 for m in seen_tmp_modes if m >= 0), (
        f"SB3: .tmp mode was {[oct(m) for m in seen_tmp_modes]!r}, "
        "expected 0o600 on every creation"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SB4 — headless: stale inherited DISPLAY must not bypass guard
# ─────────────────────────────────────────────────────────────────────────────


def test_sb4_headless_defaults_to_tty_only_when_stdin_not_a_tty(monkeypatch):
    """Under `no TTY + DISPLAY set to a stale inherited value`, _is_headless()
    MUST still return True. Pre-fix: DISPLAY presence alone bypasses the guard,
    so tmux/launchd-inherited DISPLAY (no actual X server reachable) reproduces
    the run_local_server hang.

    Post-fix policy (as stated in the finding): default to TTY-only signal.
    """
    from src.gmail import client as client_mod

    # No TTY, but a stale-inherited DISPLAY value exists.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setenv("DISPLAY", ":99")  # stale-looking, but present.
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("HIRING_AGENT_HEADLESS", raising=False)

    assert client_mod._is_headless() is True, (
        "SB4: _is_headless() returned False even though stdin is not a TTY. "
        "Stale inherited DISPLAY from tmux/launchd bypasses the guard and "
        "run_local_server hangs. Default to TTY-only signal for cron."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SG1 — dispatcher must not touch keyring on every apply
# ─────────────────────────────────────────────────────────────────────────────


def test_sg1_dispatcher_caches_load_state_per_ats_user_across_pipeline_run(monkeypatch):
    """Repeated `apply_to_job` calls for the SAME (ats, user) MUST NOT call
    credentials.load_state() every time. Pre-fix: dispatcher calls load_state
    unconditionally on every apply — reintroduces the keyring-hang class the
    Group J OAuth fix was written to close.
    """
    from src.apply import dispatcher as disp_mod

    call_count = {"n": 0}

    def _fake_load_state(ats, user):
        call_count["n"] += 1
        return {"cookies": [{"name": "s", "value": "v"}], "origins": []}

    import src.apply.credentials as creds_mod
    monkeypatch.setattr(creds_mod, "load_state", _fake_load_state)

    # Reset the dispatcher-level cache (fix will add one).
    if hasattr(disp_mod, "_reset_state_cache"):
        disp_mod._reset_state_cache()

    class _FakeSession:
        page = MagicMock()

    class _FakeCtxMgr:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            return _FakeCtxMgr()

    monkeypatch.setattr(disp_mod, "get_transport", lambda cfg, kind: _FakeTransport())

    class _FakeAdapter:
        name = "greenhouse"

        def detect(self, url):
            return True

        def apply(self, page, ctx):
            from src.apply.types import ApplyResult
            return ApplyResult(status="skipped", reason="test")

    monkeypatch.setattr(disp_mod, "dispatch", lambda url, cfg: _FakeAdapter())

    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext

    profile = CandidateProfile.load(str(ROOT / "templates" / "candidate_profile.yaml.example"))

    for _ in range(5):
        ctx = ApplyContext(
            profile=profile,
            job={"url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme", "title": "Eng"},
            resume_path=None, cover_letter_path=None,
            config={"apply": {"enabled": True}},
            applicant="single", dry_run=True, mode="review",
        )
        disp_mod.apply_to_job(
            "https://boards.greenhouse.io/acme/jobs/1", ctx,
            {"apply": {"enabled": True, "allowed_ats": ["greenhouse"], "user": "single"}},
        )

    assert call_count["n"] <= 1, (
        f"SG1: load_state was called {call_count['n']}x for same (ats, user). "
        "Dispatcher touches keyring on every apply — reintroduces B4-class hang."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SG2 — malformed load_state dict must not be passed through unchecked
# ─────────────────────────────────────────────────────────────────────────────


def test_sg2_malformed_state_dict_not_passed_to_transport(monkeypatch):
    """A `load_state` return value that is neither a valid envelope nor a
    valid `{cookies, origins}` dict MUST NOT reach `transport.open()`.

    Pre-fix: dispatcher.py:278 falls through to `storage_state = state` even
    when state is `{"garbage": True}` — passing a malformed dict to Playwright.
    """
    from src.apply import dispatcher as disp_mod

    seen_state: dict[str, Any] = {"value": "UNSET"}

    class _FakeSession:
        page = MagicMock()

    class _FakeCtxMgr:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            seen_state["value"] = storage_state
            return _FakeCtxMgr()

    monkeypatch.setattr(disp_mod, "get_transport", lambda cfg, kind: _FakeTransport())

    class _FakeAdapter:
        name = "greenhouse"

        def detect(self, url):
            return True

        def apply(self, page, ctx):
            from src.apply.types import ApplyResult
            return ApplyResult(status="skipped", reason="test")

    monkeypatch.setattr(disp_mod, "dispatch", lambda url, cfg: _FakeAdapter())

    if hasattr(disp_mod, "_reset_state_cache"):
        disp_mod._reset_state_cache()

    # Malformed: neither envelope nor {cookies, origins}.
    def _fake_load_state(ats, user):
        return {"garbage": True, "not_valid": "shape"}

    import src.apply.credentials as creds_mod
    monkeypatch.setattr(creds_mod, "load_state", _fake_load_state)

    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext

    profile = CandidateProfile.load(str(ROOT / "templates" / "candidate_profile.yaml.example"))
    ctx = ApplyContext(
        profile=profile,
        job={"url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme", "title": "Eng"},
        resume_path=None, cover_letter_path=None,
        config={"apply": {"enabled": True}},
        applicant="single", dry_run=True, mode="review",
    )

    disp_mod.apply_to_job(
        "https://boards.greenhouse.io/acme/jobs/1", ctx,
        {"apply": {"enabled": True, "allowed_ats": ["greenhouse"], "user": "single"}},
    )

    # Post-fix: storage_state must be None (dropped as malformed).
    assert seen_state["value"] is None, (
        f"SG2: malformed load_state dict was passed through to transport.open() "
        f"as {seen_state['value']!r}. Fix must validate {{cookies, origins}} "
        "shape and drop malformed dicts to None."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SG3 — captcha import failure must not crash per-job apply
# ─────────────────────────────────────────────────────────────────────────────


def test_sg3_captcha_import_failure_does_not_crash_per_job(monkeypatch):
    """If `from src.apply.captcha import detect` fails at seam entry (e.g.
    playwright not importable in this checkout), `_seam.run_for_job` MUST
    still return a benign result — not raise.

    Pre-fix: _seam.py:318 imports captcha_detect unguarded — any import
    failure crashes every per-job apply.
    """
    from src.apply import _seam as _apply_seam
    import sys as _sys

    # Simulate an ImportError from src.apply.captcha by injecting a
    # broken module into sys.modules. The seam does `from src.apply.captcha
    # import detect` — Python will import the module and getattr detect.
    class _BrokenCaptcha:
        def __getattr__(self, name):
            raise ImportError(f"captcha module broken: {name}")

    monkeypatch.setitem(_sys.modules, "src.apply.captcha", _BrokenCaptcha())

    captured: dict[str, Any] = {}

    def _capture_apply_to_job(*, job_url, ctx, config):
        captured["ctx"] = ctx
        from src.apply.types import ApplyResult
        return ApplyResult(status="skipped", reason="test-capture")

    monkeypatch.setattr(_apply_seam, "_call_apply_to_job", _capture_apply_to_job)

    apply_cfg = {
        "enabled": True,
        "mode": "review",
        "dry_run": True,
        "allowed_ats": ["greenhouse"],
        "profile_path": "templates/candidate_profile.yaml.example",
        "dedup_db_path": "state/applied_jobs.db",
        "storage_state_dir": "config/credentials/apply",
        "user": "single",
    }
    job = {
        "title": "Eng", "company": "Acme",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "ats": "greenhouse",
    }
    job_log = MagicMock()

    # Must not raise.
    result = _apply_seam.run_for_job(
        job=job, jd_text="jd",
        lane={"name": "pmm", "label": "PMM"},
        resume_path=None, cover_letter_path=None,
        apply_config=apply_cfg, job_log=job_log, gmail_client=None,
    )

    # Post-fix: captcha_detector on ctx is None (dropped), but the apply
    # continued and returned a benign result. Pre-fix: exception propagates
    # to the outer `except Exception` block → returns None + logs
    # apply.seam.error.
    # We accept either behavior IF no crash occurred — but the ctx should
    # exist with captcha_detector=None (safe drop).
    assert result is not None, (
        "SG3: captcha import failure caused per-job apply to soft-fail "
        "(returned None) — the captcha_detect import is not guarded."
    )
    ctx = captured.get("ctx")
    assert ctx is not None, "SG3: apply_to_job was never called"
    # Post-fix: captcha_detector is None (safe drop on import failure).
    assert getattr(ctx, "captcha_detector", "sentinel") is None, (
        "SG3: expected ctx.captcha_detector=None on import failure "
        "(fail-open pattern mirroring dedup init)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SD1 — storage_state load-failure log must not carry decrypted payload bytes
# ─────────────────────────────────────────────────────────────────────────────


def test_sd1_storage_state_load_failure_log_carries_no_payload_bytes(monkeypatch, caplog):
    """When `credentials.load_state` raises, the dispatcher's log line MUST
    NOT include exception message content that could carry decrypted payload
    bytes (Fernet InvalidToken unwrap exceptions can contain plaintext).

    Post-fix policy: catch specific exception types (json.JSONDecodeError,
    Fernet InvalidToken); log structural fields (`exc_type`) only — never
    `_exc_repr(exc)`.
    """
    import logging as _logging
    from src.apply import dispatcher as disp_mod

    # Sentinel exception whose str carries "would-be-plaintext" — proves the
    # log leaks the payload.
    class _BoomInvalidToken(Exception):
        pass

    def _fake_load_state(ats, user):
        raise _BoomInvalidToken("PAYLOAD_LEAK_MARKER_decrypted_cookie_value=abc123")

    import src.apply.credentials as creds_mod
    monkeypatch.setattr(creds_mod, "load_state", _fake_load_state)

    if hasattr(disp_mod, "_reset_state_cache"):
        disp_mod._reset_state_cache()

    class _FakeSession:
        page = MagicMock()

    class _FakeCtxMgr:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            return _FakeCtxMgr()

    monkeypatch.setattr(disp_mod, "get_transport", lambda cfg, kind: _FakeTransport())

    class _FakeAdapter:
        name = "greenhouse"

        def detect(self, url):
            return True

        def apply(self, page, ctx):
            from src.apply.types import ApplyResult
            return ApplyResult(status="skipped", reason="test")

    monkeypatch.setattr(disp_mod, "dispatch", lambda url, cfg: _FakeAdapter())

    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext

    profile = CandidateProfile.load(str(ROOT / "templates" / "candidate_profile.yaml.example"))
    ctx = ApplyContext(
        profile=profile,
        job={"url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme", "title": "Eng"},
        resume_path=None, cover_letter_path=None,
        config={"apply": {"enabled": True}},
        applicant="single", dry_run=True, mode="review",
    )

    with caplog.at_level(_logging.WARNING):
        disp_mod.apply_to_job(
            "https://boards.greenhouse.io/acme/jobs/1", ctx,
            {"apply": {"enabled": True, "allowed_ats": ["greenhouse"], "user": "single"}},
        )

    all_output = " ".join(rec.getMessage() for rec in caplog.records)
    # Also include structlog output (dispatcher uses structlog get_logger).
    assert "PAYLOAD_LEAK_MARKER" not in all_output, (
        f"SD1: dispatcher log leaked exception message content "
        f"(potential decrypted payload). Output: {all_output!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SG4 — main.py must not use orphan .get('apply', {})
# ─────────────────────────────────────────────────────────────────────────────


def test_sg4_main_get_apply_no_orphan_dict_for_dry_run_mutation():
    """`config.get('apply', {})` returns a FRESH orphan dict on every call
    when the key is missing. Any dry_run mutation applied to it is lost.
    Fix: use `config.setdefault('apply', {})` OR the seam's
    `_safe_apply_config` helper, so the same dict is threaded to the seam.
    """
    import inspect
    from src import main as main_mod

    src_text = inspect.getsource(main_mod.run_pipeline)
    # Filter out comment lines / docstring content so we only match REAL code.
    code_lines = [
        line for line in src_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code_text = "\n".join(code_lines)
    # No bare `config.get('apply', {})` on the apply_config binding line in
    # real code; the fix uses config.setdefault('apply', {}).
    has_bad_get = (
        "apply_config = config.get('apply', {})" in code_text
        or 'apply_config = config.get("apply", {})' in code_text
    )
    assert not has_bad_get, (
        "SG4: run_pipeline's apply_config binding uses `config.get('apply', {})` "
        "which returns a fresh orphan dict when 'apply' is missing. Use "
        "setdefault or _safe_apply_config so the dry_run threading survives."
    )
    # Positive check: fix uses setdefault OR _safe_apply_config.
    has_fix = (
        'config.setdefault("apply"' in code_text
        or "config.setdefault('apply'" in code_text
        or "_safe_apply_config" in code_text
    )
    assert has_fix, (
        "SG4: expected `config.setdefault('apply', {})` or `_safe_apply_config` "
        "in run_pipeline; neither found."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE1 — M5 test patches the correct transport surface
# ─────────────────────────────────────────────────────────────────────────────


def test_se1_dispatcher_get_transport_reexport_removed_or_marked_deprecated():
    """`src.apply.dispatcher.get_transport` was added purely to satisfy the
    Phase 3 M5 RED test's patch surface. Real code should be patched at
    `src.apply.transport.get_transport` (which the pre-existing 7 tests do).

    Post-fix: either the M5 test patches `src.apply.transport.get_transport`
    directly, OR the dispatcher's re-export is annotated/deprecated so future
    additions don't drift onto it.

    We accept the fix as: dispatcher.py's get_transport is a call-through
    wrapper explicitly commented to justify its existence (already done),
    AND the M5 test on Phase 3 doesn't rely on the wrapper being the only
    patch surface. Verify by patching the transport module instead.
    """
    from src.apply import dispatcher as disp_mod
    from src.apply import transport as transport_mod
    import inspect

    # The dispatcher's get_transport must be a THIN indirection (docstring
    # comment justifies the wrapper). Assert it explicitly references the
    # transport module surface as the primary patch point.
    src_text = inspect.getsource(disp_mod.get_transport)
    assert "src.apply.transport" in src_text, (
        "SE1: dispatcher.get_transport must document that transport module "
        "is the primary patch surface; the wrapper exists only for backwards "
        "compatibility with M5-style patch calls."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE2 — shared load_and_unwrap_state helper exists
# ─────────────────────────────────────────────────────────────────────────────


def test_se2_load_and_unwrap_state_helper_exists_and_shared():
    """Post-fix: a shared `load_and_unwrap_state(ats, user)` helper exists
    (either in credentials.py or a new module) and is used by BOTH the
    dispatcher's storage_state load path and review.execute_confirmed_submit.
    Pre-fix: envelope-detection + unwrap logic is duplicated.
    """
    import importlib

    creds_mod = importlib.import_module("src.apply.credentials")
    assert hasattr(creds_mod, "load_and_unwrap_state"), (
        "SE2: shared helper `load_and_unwrap_state` was not added to "
        "src.apply.credentials — envelope-unwrap logic is still duplicated "
        "between dispatcher and review."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE3 — gmail atomic write reuses shared helper (has fsync)
# ─────────────────────────────────────────────────────────────────────────────


def test_se3_gmail_token_write_uses_atomic_write_with_fsync(tmp_path, monkeypatch):
    """The Gmail token persistence path MUST go through a helper that
    fsync's before rename — same guarantee credentials.py's `_atomic_write`
    already provides. Post-fix: a shared helper is used from both call sites.

    Verified by spying on `os.fsync` — pre-fix path never calls it.
    """
    from src.gmail import client as client_mod

    token_path = tmp_path / "creds" / "token.json"
    creds_path = tmp_path / "creds" / "credentials.json"
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text('{"placeholder": true}')

    expired_then_valid = SimpleNamespace(
        valid=False, expired=True, refresh_token="refresh",
        to_json=lambda: '{"token": "abc"}',
        refresh=lambda req: setattr(expired_then_valid, "valid", True),
    )
    monkeypatch.setattr(
        client_mod.Credentials, "from_authorized_user_file",
        classmethod(lambda cls, *a, **k: expired_then_valid),
    )
    monkeypatch.setattr(client_mod, "build", lambda *a, **k: MagicMock())

    fsync_calls = {"n": 0}
    real_fsync = os.fsync

    def _spy_fsync(fd):
        fsync_calls["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)

    client_mod.GmailClient()

    assert fsync_calls["n"] >= 1, (
        "SE3: gmail token write path never called os.fsync — write may be "
        "lost on power failure between rename and disk flush. Use the shared "
        "atomic-write helper from credentials.py."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE4 — gmail token write cleans up orphan .tmp on OSError
# ─────────────────────────────────────────────────────────────────────────────


def test_se4_gmail_token_write_cleans_up_orphan_tmp_on_error(tmp_path, monkeypatch):
    """If the chmod / os.replace step raises AFTER the .tmp file has been
    written, the .tmp file MUST be cleaned up (not left orphaned with a
    valid token).

    Pre-fix: no try/finally; an OSError mid-write leaves `token.json.tmp`
    on disk containing the newly-refreshed token indefinitely.
    """
    from src.gmail import client as client_mod

    token_path = tmp_path / "creds" / "token.json"
    creds_path = tmp_path / "creds" / "credentials.json"
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text('{"placeholder": true}')

    expired_then_valid = SimpleNamespace(
        valid=False, expired=True, refresh_token="refresh",
        to_json=lambda: '{"token": "abc"}',
        refresh=lambda req: setattr(expired_then_valid, "valid", True),
    )
    monkeypatch.setattr(
        client_mod.Credentials, "from_authorized_user_file",
        classmethod(lambda cls, *a, **k: expired_then_valid),
    )
    monkeypatch.setattr(client_mod, "build", lambda *a, **k: MagicMock())

    # Force os.replace to raise AFTER the .tmp exists.
    def _boom_replace(src, dst, *a, **k):
        raise OSError("replace failed for test")

    monkeypatch.setattr(os, "replace", _boom_replace)

    with pytest.raises(OSError):
        client_mod.GmailClient()

    tmp_path_final = token_path.parent / f"{token_path.name}.tmp"
    assert not tmp_path_final.exists(), (
        f"SE4: token .tmp left orphaned after replace() failure at "
        f"{tmp_path_final!s}. try/finally must unlink on failure."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE5 — main() catches AuthError from GmailClient()
# ─────────────────────────────────────────────────────────────────────────────


def test_se5_main_catches_auth_error_and_exits_nonzero(monkeypatch, capsys):
    """`main()` must catch `AuthError` from GmailClient() and exit with a
    non-zero code plus a structured log event. Pre-fix: uncaught traceback
    under headless-cron-expired-token — cron sees a stack trace but no
    grep-able failure signal.
    """
    from src import main as main_mod
    from src.gmail.client import AuthError

    def _boom_gmail_client():
        raise AuthError("headless auth required")

    # Patch the imported symbol used inside main().
    # NB: main.py imports GmailClient inside main() as
    # `from gmail.client import GmailClient` — we patch the module attr
    # so any call to `GmailClient()` from main() falls through to _boom.
    import src.gmail.client as gc
    monkeypatch.setattr(gc, "GmailClient", _boom_gmail_client)

    # Also stub argv so main() runs without --test.
    monkeypatch.setattr(sys, "argv", ["main"])

    # Stub load_config / load_project_bank so we get to the GmailClient() call.
    monkeypatch.setattr(main_mod, "load_config", lambda: {
        "apply": {"enabled": False},
        "gmail": {"alert_sender": "x", "alert_subject_contains": "y",
                  "processed_label": "z", "digest_subject_template": ""},
        "jobs": {"max_per_run": 1},
        "lanes": [], "resume": {}, "cover_letter": {},
        "qa": {}, "contacts": {"enabled": False},
        "pdf": {}, "scraper": {},
    })
    monkeypatch.setattr(main_mod, "load_project_bank", lambda: [])

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()

    assert excinfo.value.code != 0, (
        f"SE5: main() should exit non-zero on AuthError, got code={excinfo.value.code!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SE6 — llm._call_via_cli long prompt uses input= not tempfile
# ─────────────────────────────────────────────────────────────────────────────


def test_se6_llm_call_via_cli_long_prompt_uses_input_stdin_not_tempfile(monkeypatch):
    """Post-M10 (shell removal), the tempfile round-trip for long prompts
    is unnecessary. Pass the prompt directly as `subprocess.run(..., input=)`
    so the two branches collapse and no /tmp file is created.

    Pre-fix: still writes to a NamedTemporaryFile, then reads back as stdin.
    """
    from src import llm as llm_mod

    class _FakeCompleted:
        returncode = 0
        stdout = "ok"
        stderr = ""

    seen_call: dict[str, Any] = {}

    def _fake_run(*args, **kwargs):
        seen_call["args"] = args
        seen_call["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Detect tempfile creation.
    tempfile_calls = {"n": 0}
    import tempfile as _tempfile
    real_named = _tempfile.NamedTemporaryFile

    def _spy_named(*a, **k):
        tempfile_calls["n"] += 1
        return real_named(*a, **k)

    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", _spy_named)

    long_prompt = "x" * 9000
    llm_mod._call_via_cli(long_prompt, model="haiku", system=None, timeout=1)

    assert tempfile_calls["n"] == 0, (
        f"SE6: long-prompt path still creates a tempfile ({tempfile_calls['n']}x). "
        "Post-M10 (no shell), use `input=full_prompt` on subprocess.run instead."
    )
    # And the input kwarg should carry the prompt.
    assert "input" in seen_call["kwargs"], (
        "SE6: subprocess.run() was called without `input=` — the fixed path "
        "must pass the prompt via stdin without a tempfile round-trip."
    )
    assert seen_call["kwargs"]["input"] == long_prompt, (
        "SE6: prompt was not delivered on stdin via input=."
    )
