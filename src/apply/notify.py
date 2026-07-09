"""
S13 — fast-path emailer.

Two never-blocking alert surfaces fire the moment the pipeline hits a state a
human must resolve before the next daily digest:

  - notify_captcha_escalation: CAPTCHA detected, run escalated / bailed.
  - notify_session_expired:    per-ATS storage_state came back stale.

Both land in the operator's inbox with subject prefix `[hiring-agent] URGENT:`
and never raise back to the caller — a send failure is logged (event
`notify.send_failed`) and swallowed so the pipeline keeps processing.

Recipient is resolved from `apply.fast_path_recipient` (default
`env:MY_EMAIL`) at CALL TIME — never at import time — so test isolation and
reload semantics hold.

Landmine guards enforced by construction:
  - L6: uses `datetime.now(timezone.utc)`; the deprecated tz-naive helper is
        never called (nor named — a test greps the source for it).
  - L7: candidate PII (email, phone, name, resume text) never touched — this
        module only reads job metadata off ctx (ats, company, role_title,
        job_url, apply_url) and the CAPTCHA kind + review URL. The Gmail
        address in `notify_session_expired` is logged only as a
        sha256-truncated user_hash (Gmail addresses count as PII per L7).
  - L8: no ad-hoc retry loops here — send_immediate handles the single retry
        via GmailClient._retry_call; this module only catches and swallows.

Scope: fast-path lane only. Daily rollup is S14 (digest).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from src.gmail.client import AuthError, GmailClient

if TYPE_CHECKING:
    from src.apply.captcha import CaptchaKind
    from src.apply.types import ApplyContext


__all__ = ["notify_captcha_escalation", "notify_session_expired"]

_log = structlog.get_logger(__name__)

_URGENT_PREFIX = "[hiring-agent] URGENT:"


# ---------------------------------------------------------------------------
# Recipient resolution — CALL-TIME reads only
# ---------------------------------------------------------------------------

def _resolve_recipient(config: dict | None = None) -> str | None:
    """
    Return the fast-path recipient email address, or None if unresolvable.

    Reads `apply.fast_path_recipient` from `config` if provided; otherwise
    falls back to the documented default `env:MY_EMAIL`. Values prefixed
    `env:` are dereferenced via `os.environ` at call time.

    Emits `notify.recipient_unresolved` and returns None when the referenced
    env var is missing or the resolved value is empty; callers swallow.
    """
    default = "env:MY_EMAIL"
    value = default
    if config:
        apply_cfg = config.get("apply") if isinstance(config, dict) else None
        if isinstance(apply_cfg, dict):
            candidate = apply_cfg.get("fast_path_recipient")
            if isinstance(candidate, str) and candidate.strip():
                value = candidate.strip()

    if value.startswith("env:"):
        env_var = value[len("env:") :]
        resolved = os.environ.get(env_var, "").strip()
        if not resolved:
            _log.info("notify.recipient_unresolved", env_var=env_var)
            return None
        return resolved

    # Literal address form.
    if not value.strip():
        _log.info("notify.recipient_unresolved", reason="empty_literal")
        return None
    return value.strip()


# ---------------------------------------------------------------------------
# Send primitive — constructs the client and delegates; NEVER raises
# ---------------------------------------------------------------------------

def _send(subject: str, body: str, to: str) -> None:
    """
    Send one urgent email via GmailClient.send_immediate.

    Any exception bubbling out of GmailClient (auth failure, HttpError after
    the internal retry, unexpected error) is caught by the caller
    (`notify_captcha_escalation` / `notify_session_expired`), which owns the
    "never raise" contract. This function stays honest and does NOT swallow
    itself — the layering keeps the client sender-code-review-clean.
    """
    client = GmailClient()
    client.send_immediate(subject=subject, body=body, to=to)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_captcha_escalation(
    ctx: "ApplyContext",
    kind: "CaptchaKind",
    review_url: str | None,
) -> None:
    """
    Fire an urgent email when a CAPTCHA has been detected.

    Never raises. On send failure logs `notify.send_failed` once with kv
    `{ats, kind, http_status}` and returns None. On missing MY_EMAIL logs
    `notify.recipient_unresolved` and returns None. On success logs
    `notify.captcha.sent` with `{ats, kind, review_url_present}` — the URL
    itself is never raw-logged.
    """
    ats = getattr(ctx, "ats", None) or "unknown"

    # Resolve recipient at CALL time — read from ctx.config if present.
    config = getattr(ctx, "config", None)
    recipient = _resolve_recipient(config)
    if recipient is None:
        return None

    company = getattr(ctx, "company", None) or ""
    role_title = getattr(ctx, "role_title", None) or ""
    job_url = getattr(ctx, "job_url", None) or ""
    apply_url = getattr(ctx, "apply_url", None)

    detected_at = datetime.now(timezone.utc).isoformat()

    subject = f"{_URGENT_PREFIX} CAPTCHA detected on {ats}"

    review_line = (
        f"Browserbase replay: {review_url}"
        if review_url
        else "Browserbase replay: n/a"
    )

    lines = [
        f"ats:           {ats}",
        f"company:       {company}",
        f"role_title:    {role_title}",
        f"job_url:       {job_url}",
    ]
    if apply_url:
        lines.append(f"apply_url:     {apply_url}")
    lines.extend(
        [
            f"captcha_kind:  {kind}",
            review_line,
            f"detected_at:   {detected_at}",
        ]
    )
    body = "\n".join(lines) + "\n"

    try:
        _send(subject, body, recipient)
    except AuthError:
        # I2-B4 (Phase 3 xhigh iter-2): distinct signal for auth-required.
        # Pre-fix the AuthError from GmailClient() was folded into the
        # generic `notify.send_failed` event, so an operator watching for
        # URGENT captcha email failures could not distinguish "gmail send
        # failed" from "gmail auth is DEAD". Post-fix: dedicated event.
        _log.warning(
            "notify.auth_required",
            ats=ats,
            kind=kind,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never-blocking by contract
        http_status = _extract_http_status(exc)
        _log.info(
            "notify.send_failed",
            ats=ats,
            kind=kind,
            http_status=http_status,
        )
        return None

    _log.info(
        "notify.captcha.sent",
        ats=ats,
        kind=kind,
        review_url_present=review_url is not None,
    )
    return None


def notify_session_expired(
    ats: str,
    user: str,
    last_run_iso: str | None,
    config: dict | None = None,
) -> None:
    """
    Fire an urgent email when a per-ATS storage_state came back stale.

    Never raises. `user` (a Gmail address) is written to the email body (so
    the operator knows which account to re-authenticate) but is NEVER
    raw-logged — logs carry only a sha256-truncated `user_hash`. On send
    failure logs `notify.send_failed` and returns None.

    `config` is optional (spec §41 pinned the required-positional signature
    to `(ats, user, last_run_iso)`); when passed it is used to honor
    `apply.fast_path_recipient` uniformly with the captcha path — otherwise
    the resolver defaults to `env:MY_EMAIL` per master-plan §4.7.
    """
    recipient = _resolve_recipient(config)
    if recipient is None:
        return None

    user_hash = hashlib.sha256(user.encode()).hexdigest()[:12]
    detected_at = datetime.now(timezone.utc).isoformat()

    subject = f"{_URGENT_PREFIX} session expired for {ats}"

    lines = [
        f"ats:            {ats}",
        f"user:           {user}",
        f"last_run_iso:   {last_run_iso if last_run_iso is not None else 'n/a'}",
        f"detected_at:    {detected_at}",
        "",
        f"Run: python -m src.apply.bootstrap {ats}",
    ]
    body = "\n".join(lines) + "\n"

    try:
        _send(subject, body, recipient)
    except AuthError:
        # I2-B4 (Phase 3 xhigh iter-2): distinct signal — see captcha path.
        _log.warning(
            "notify.auth_required",
            ats=ats,
            kind="session_expired",
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never-blocking by contract
        http_status = _extract_http_status(exc)
        _log.info(
            "notify.send_failed",
            ats=ats,
            kind="session_expired",
            http_status=http_status,
        )
        return None

    _log.info(
        "notify.session_expired.sent",
        ats=ats,
        user_hash=user_hash,
    )
    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _extract_http_status(exc: BaseException) -> Any:
    """Pull an HTTP status off googleapiclient.errors.HttpError, else None."""
    resp = getattr(exc, "resp", None)
    if resp is not None:
        status = getattr(resp, "status", None)
        if status is not None:
            return status
    return None
