"""Phase 3 xhigh iter-2 — RED tests for regressions/gaps surfaced by the
iter-1 review sweep. Groups:

Iter-2 findings:
    I2-B1  SB2 regression: dry_run does not reach _AutoModeCtx via review loop.
    I2-B2  SE5 gap: main() does not catch google.auth RefreshError.
    I2-B3  LocalTransport drops storage_state dict — M5/SG1 plumbing does
           not reach local mode (BrowserbaseTransport only).
    I2-B4  notify.py's GmailClient() swallows AuthError as
           `notify.send_failed` — operator loses distinct auth-required signal.
    I2-B5  atomic_write_text is defined but not used from gmail/client.py.
    I2-B6  SB1 log leak: `error=str(exc)` when scrubber install failed —
           scrubber is guaranteed inactive on this exact log line.
    I2-B7  Env-var truthiness pitfall: HIRING_AGENT_HEADLESS=0 evaluates True.
    I2-B8  _state_cache caches transient StorageStateBackendError as None,
           poisoning the whole pipeline after a single transient blip.
    I2-B9  Conventions/SD1: new `error=str(exc)` log lines in _seam.py + main.py
           should use `exc_type=type(exc).__name__` per iter-1 SD1 pattern.
"""
from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def _reset_dispatcher_state_cache():
    """SG1 cache reset — same guard as iter-1."""
    try:
        from src.apply.dispatcher import _reset_state_cache
        _reset_state_cache()
    except ImportError:
        pass
    yield
    try:
        from src.apply.dispatcher import _reset_state_cache
        _reset_state_cache()
    except ImportError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# I2-B1 — SB2 regression: dry_run must reach the review-loop YES branch
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b1_cli_dry_run_reaches_execute_confirmed_submit_auto_mode_ctx():
    """When `run_pipeline(dry_run=True)` is invoked (i.e. `--dry-run` or
    `--test` at the CLI), the effective dry_run flag MUST reach
    `execute_confirmed_submit`'s `_AutoModeCtx.dry_run`. Pre-iter-1 the
    apply_config['dry_run']=True mutation delivered this; iter-1 dropped
    the mutation but did NOT thread dry_run through initialize() → the
    review-loop YES branch now sees `dry_run=False` and would click Submit
    for real, even under --dry-run.
    """
    from src.apply.review import _AutoModeCtx

    # Simulate the pre-fix behavior: config carries dry_run: false, but the
    # operator ran --dry-run at the CLI. Post-fix: an effective dry_run
    # override must reach _AutoModeCtx via a wiring path we can inspect.
    #
    # RED: build a fake decision + config, construct _AutoModeCtx as
    # execute_confirmed_submit would (from the review-loop invocation),
    # and assert dry_run is True.
    decision = SimpleNamespace(
        review_id="rid",
        ats="greenhouse",
        applicant="single",
        company="Acme",
        role_title="Eng",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    # After the fix, execute_confirmed_submit accepts an explicit `dry_run`
    # kwarg; when not passed, it reads config['apply']['dry_run']. The seam
    # threads the CLI dry_run through it.
    from src.apply import review as review_mod
    import inspect
    sig = inspect.signature(review_mod.execute_confirmed_submit)
    assert "dry_run" in sig.parameters, (
        "I2-B1: execute_confirmed_submit does not accept a `dry_run` kwarg. "
        "The SB2 fix removed the apply_config['dry_run']=True mutation but "
        "did not add a threading path — the review-loop YES branch will "
        "run a real submit under --dry-run."
    )

    # Also verify the seam's initialize() accepts a dry_run kwarg so the
    # threading path exists all the way from run_pipeline → initialize →
    # poll_pending_reviews → execute_confirmed_submit.
    from src.apply import _seam as _apply_seam
    init_sig = inspect.signature(_apply_seam.initialize)
    assert "dry_run" in init_sig.parameters, (
        "I2-B1: _seam.initialize does not accept `dry_run`. Without this, "
        "run_pipeline(dry_run=True) cannot reach the review-loop poll."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B2 — SE5 gap: RefreshError must also trigger clean exit
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b2_main_catches_refresh_error_from_gmail_client(monkeypatch, capsys):
    """`google.auth.exceptions.RefreshError` from `creds.refresh(Request())`
    MUST also be treated as auth-required — SE5's fix only catches AuthError,
    but the same failure class (expired-token cron) manifests as RefreshError
    when the token is present but the refresh grant has been revoked (60-day
    inactivity, security event, scope change).
    """
    from src import main as main_mod

    # Trigger the bootstrap so gmail.client is importable.
    _ = main_mod
    from google.auth.exceptions import RefreshError  # noqa: E402

    def _boom():
        raise RefreshError("invalid_grant: Bad Request", {"error": "invalid_grant"})

    # Patch both module slots.
    import src.gmail.client as gc_src
    monkeypatch.setattr(gc_src, "GmailClient", _boom)
    try:
        import gmail.client as gc_bare  # type: ignore[import-not-found]
        monkeypatch.setattr(gc_bare, "GmailClient", _boom)
    except ImportError:
        pass

    monkeypatch.setattr(sys, "argv", ["main"])
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
        f"I2-B2: main() must exit non-zero on RefreshError too, got code={excinfo.value.code!r}. "
        "SE5's AuthError catch scope is too narrow — an expired-refresh-token cron "
        "still traces uncaught."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B3 — LocalTransport materializes storage_state dict
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b3_local_transport_materializes_storage_state_dict(monkeypatch, tmp_path):
    """LocalTransport.open MUST materialize a passed storage_state dict into
    a temp file and forward it to `browser.session(storage_state_path=...)`.

    Pre-fix: dict is 'protocol-conformance sugar only' and dropped — the
    M5/SG1 fix threads storage_state everywhere on the dispatcher side but
    LocalTransport still opens an anonymous browser. Every bootstrapped
    local-mode apply runs unauthenticated.
    """
    from src.apply.transport import local as local_mod

    seen_storage_path: dict[str, Any] = {"value": "UNSET"}

    class _FakePage:
        def goto(self, url):
            pass

    class _FakeSessionCM:
        def __enter__(self):
            return (_FakePage(), None)

        def __exit__(self, *exc):
            return False

    class _FakeBrowser:
        def session(self, headless=True, storage_state_path=None):
            seen_storage_path["value"] = storage_state_path
            return _FakeSessionCM()

    monkeypatch.setitem(sys.modules, "browser", _FakeBrowser())

    transport = local_mod.LocalTransport()
    state_dict = {"cookies": [{"name": "s", "value": "v"}], "origins": []}
    with transport.open("https://example.com", storage_state=state_dict):
        pass

    ss_path = seen_storage_path["value"]
    assert ss_path is not None, (
        "I2-B3: LocalTransport dropped storage_state dict — passed None to "
        "browser.session(storage_state_path=). The M5/SG1 dispatcher fix "
        "threads state everywhere BUT the local transport, so bootstrapped "
        "credentials never actually load in local mode."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B4 — notify.py distinguishes AuthError from generic send_failed
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b4_notify_captcha_escalation_surfaces_auth_error_distinctly(monkeypatch):
    """When `_send()` in notify.py raises AuthError during
    notify_captcha_escalation (headless cron, expired token), the log
    event MUST be `notify.auth_required` (or similar distinct name),
    NOT the generic `notify.send_failed`. Operators watching for URGENT
    captcha email failures otherwise see a generic HTTP-like failure
    and never realize auth has fully lapsed.
    """
    import src.apply.notify as notify_mod
    # Use src.gmail.client.AuthError — notify.py imports AuthError from
    # `src.gmail.client`, so the raised exception MUST be the same module
    # slot for the except-clause to match (dual-module trap: bootstrap in
    # main puts `src/` on sys.path so `gmail.client` and `src.gmail.client`
    # are DIFFERENT module objects).
    from src.gmail.client import AuthError

    # Force the underlying GmailClient() to raise AuthError inside _send.
    def _boom_client():
        raise AuthError("Gmail OAuth requires an interactive browser login")

    monkeypatch.setattr(notify_mod, "GmailClient", _boom_client)

    logged: list[tuple[str, dict]] = []

    class _CapturingLog:
        def info(self, event, **kw):
            logged.append((event, kw))
        def warning(self, event, **kw):
            logged.append((event, kw))
        def error(self, event, **kw):
            logged.append((event, kw))

    monkeypatch.setattr(notify_mod, "_log", _CapturingLog())

    # Call notify_captcha_escalation — signature is (ctx, kind, review_url).
    # Build a minimal ctx-shape SimpleNamespace with just the getattr-required
    # fields.
    monkeypatch.setenv("MY_EMAIL", "op@example.com")
    ctx = SimpleNamespace(
        ats="greenhouse",
        company="Acme",
        role_title="Eng",
        job_url="https://example.com/job",
        apply_url=None,
        config={"apply": {}},
    )
    try:
        notify_mod.notify_captcha_escalation(
            ctx=ctx,
            kind="cloudflare_turnstile",
            review_url=None,
        )
    except Exception:
        # Notify.py's swallow-and-log contract means it shouldn't raise.
        pass

    event_names = [ev for (ev, _) in logged]
    assert any("auth_required" in ev or "auth_error" in ev for ev in event_names), (
        f"I2-B4: notify caught AuthError but did not surface a distinct "
        f"auth-required event. Logged events: {event_names!r}. Operator "
        "cannot distinguish 'gmail send failed' from 'gmail auth is DEAD'."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B5 — atomic_write_text is actually used from gmail/client.py
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b5_gmail_token_write_delegates_to_atomic_write_text_helper(tmp_path, monkeypatch):
    """The Gmail token persistence path MUST route through
    `credentials.atomic_write_text` (the shared helper introduced in SE3
    specifically to unify the two atomic-write sites). Pre-iter-1: helper
    exists but gmail/client.py inlines its own copy of the logic — two
    implementations that will drift.
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

    # Spy on atomic_write_text. When called, treat as "helper used".
    from src.apply import credentials as creds_mod
    calls = {"n": 0}
    real_awt = creds_mod.atomic_write_text

    def _spy(*a, **k):
        calls["n"] += 1
        return real_awt(*a, **k)

    monkeypatch.setattr(creds_mod, "atomic_write_text", _spy)

    client_mod.GmailClient()

    assert calls["n"] >= 1, (
        "I2-B5: gmail/client.py did NOT delegate to "
        "credentials.atomic_write_text — the SE3 shared helper is a dead "
        "letter, and the two atomic-write paths will drift on future "
        "durability fixes."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B6 — SB1 log uses exc_type only (scrubber INACTIVE at this log line)
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b6_scrubber_install_failed_log_carries_no_exc_str(monkeypatch, caplog):
    """When install_scrubber fails, the resulting warning log line MUST
    NOT include `error=str(exc)` — because at this exact moment the PII
    scrubber is guaranteed inactive (that IS the failure being reported).
    Any string content of the exception can carry unredacted PII.

    Post-fix: log only `exc_type=type(exc).__name__`, mirroring SD1.
    """
    import logging as _logging
    from src.apply import _seam as _apply_seam

    class _CustomExc(Exception):
        pass

    def _boom():
        raise _CustomExc("path=/Users/PII_LEAK_MARKER/creds/state.json")

    monkeypatch.setattr(_apply_seam, "_call_install_scrubber", _boom)

    with caplog.at_level(_logging.WARNING):
        _apply_seam.initialize({"apply": {"enabled": False}}, None)

    all_output = " ".join(rec.getMessage() for rec in caplog.records)
    assert "PII_LEAK_MARKER" not in all_output, (
        f"I2-B6: scrubber-install failure log leaked exception message. "
        f"Output: {all_output!r}. Fix: log exc_type only (SD1 pattern), "
        "since the scrubber is INACTIVE at this log line."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B7 — HIRING_AGENT_HEADLESS=0 must NOT bypass guard (strict allowlist)
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b7_env_var_headless_uses_strict_allowlist(monkeypatch):
    """`HIRING_AGENT_HEADLESS=0` (a natural way to try to DISABLE the guard)
    MUST NOT be treated as True. Same for `HIRING_AGENT_INTERACTIVE_OAUTH=0`
    (natural way to try to DISABLE the opt-out).
    """
    from src.gmail import client as client_mod

    # Simulate a stdin-attached TTY so the guard's default is FALSE.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    # Case 1: HIRING_AGENT_HEADLESS='0' MUST NOT trigger guard.
    monkeypatch.setenv("HIRING_AGENT_HEADLESS", "0")
    monkeypatch.delenv("HIRING_AGENT_INTERACTIVE_OAUTH", raising=False)
    assert client_mod._is_headless() is False, (
        "I2-B7: HIRING_AGENT_HEADLESS=0 evaluates True under bare "
        "os.environ.get() truthiness — operator setting '=0' to disable "
        "the guard actually enables it. Use strict allowlist ('1','true','yes')."
    )

    # Case 2: HIRING_AGENT_INTERACTIVE_OAUTH='0' MUST NOT bypass guard.
    monkeypatch.delenv("HIRING_AGENT_HEADLESS", raising=False)
    monkeypatch.setenv("HIRING_AGENT_INTERACTIVE_OAUTH", "0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert client_mod._is_headless() is True, (
        "I2-B7: HIRING_AGENT_INTERACTIVE_OAUTH=0 disables the opt-out for "
        "operators who don't want interactive OAuth. Currently '=0' is "
        "truthy → returns False → run_local_server opens a browser."
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B8 — transient StorageStateBackendError must NOT be cached as None
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b8_dispatcher_cache_does_not_persist_transient_backend_errors(monkeypatch):
    """A transient `StorageStateBackendError` (keyring blip, DBus reload)
    on the first call MUST NOT poison the cache with None for the rest
    of the pipeline. Post-fix: distinguish "no state stored" (True None,
    cache-safe) from "backend error" (transient, do NOT cache).
    """
    from src.apply import dispatcher as disp_mod
    import src.apply.credentials as creds_mod

    call_count = {"n": 0}
    real_state = {"cookies": [{"name": "s", "value": "v"}], "origins": []}

    def _flaky_load_state(ats, user):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise creds_mod.StorageStateBackendError("transient keyring blip")
        return real_state

    monkeypatch.setattr(creds_mod, "load_state", _flaky_load_state)

    # After the fix, calling _cached_load_and_unwrap_state twice for the
    # SAME (ats, user) MUST retry on the second call (the first call
    # returned None due to transient error, which should not be cached).
    result1 = disp_mod._cached_load_and_unwrap_state("greenhouse", "single")
    result2 = disp_mod._cached_load_and_unwrap_state("greenhouse", "single")

    assert result1 is None, "Transient error → returns None. OK."
    assert result2 is not None, (
        "I2-B8: transient backend error on first call was cached as None; "
        "subsequent calls see poisoned cache instead of retrying. Every "
        "apply after a keyring blip runs unauthenticated for the rest "
        "of the pipeline. Distinguish 'no state stored' from 'backend "
        "error' and skip caching on the latter."
    )
    assert call_count["n"] == 2, (
        f"I2-B8: expected 2 calls to load_state (retry after transient), "
        f"got {call_count['n']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2-B9 — new log lines follow SD1: exc_type not error=str(exc)
# ─────────────────────────────────────────────────────────────────────────────


def test_i2b9_new_iter1_log_lines_use_exc_type_pattern():
    """New log lines added in iter-1 fixes should all use
    `exc_type=type(exc).__name__` (SD1 pattern), not `error=str(exc)` or
    `reason=str(exc)`. This is a static-source check.
    """
    import inspect
    from src.apply import _seam as _apply_seam
    from src import main as main_mod

    # _seam.initialize's new SB1 + state_cache log lines.
    src_seam = inspect.getsource(_apply_seam.initialize)
    # Find lines mentioning apply.scrubber_install_failed / state_cache_reset_failed
    # and assert exc_type present in same block, no error=str(exc).
    forbidden_pattern = 'error=str(exc)'
    if 'apply.scrubber_install_failed' in src_seam:
        # Locate the line and its neighbors.
        lines = src_seam.splitlines()
        for i, line in enumerate(lines):
            if 'apply.scrubber_install_failed' in line:
                # Check same line + next line for the forbidden pattern.
                block = line + " " + (lines[i+1] if i+1 < len(lines) else "")
                assert forbidden_pattern not in block, (
                    "I2-B9: apply.scrubber_install_failed log uses "
                    "`error=str(exc)` — SD1 pattern requires `exc_type=type(exc).__name__`. "
                    "Scrubber INACTIVE at this log line (that IS the failure)."
                )
                break

    if 'apply.state_cache_reset_failed' in src_seam:
        lines = src_seam.splitlines()
        for i, line in enumerate(lines):
            if 'apply.state_cache_reset_failed' in line:
                block = line + " " + (lines[i+1] if i+1 < len(lines) else "")
                assert forbidden_pattern not in block, (
                    "I2-B9: apply.state_cache_reset_failed log uses "
                    "`error=str(exc)` — SD1 pattern requires `exc_type=type(exc).__name__`."
                )
                break

    # main.py's gmail.auth_required event.
    src_main = inspect.getsource(main_mod.main)
    if 'gmail.auth_required' in src_main:
        forbidden_reason = 'reason=str(exc)'
        lines = src_main.splitlines()
        for i, line in enumerate(lines):
            if 'gmail.auth_required' in line:
                block = line + " " + (lines[i+1] if i+1 < len(lines) else "")
                assert forbidden_reason not in block, (
                    "I2-B9: gmail.auth_required log uses `reason=str(exc)`. "
                    "SD1 pattern requires structural fields only."
                )
                break
