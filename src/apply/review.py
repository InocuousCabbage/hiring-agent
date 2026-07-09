"""
src/apply/review.py — S12 Gmail YES/NO review-loop implementation.

Spec: .agent/one-big-feature/auto-apply-2026-07-06/03-specs/12-s12-gmail-review-loop.md

Owns:
    * ``ensure_labels`` — idempotent nested-label boot.
    * ``stage_review`` — insert ``review_pending`` row + send review email.
    * ``poll_pending_reviews`` — sweep threads, resolve YES/NO/ambiguous,
      re-ping at 24h, auto-decline at 72h.
    * ``execute_confirmed_submit`` — re-open browser via S4 session,
      re-run S8 adapter, record dedup on DOM-verified confirmation only.
    * ``Decision`` frozen dataclass.
    * ``_uuid7`` — RFC 9562 uuid7 (local impl to keep zero-new-deps).
    * ``_strip_quoted`` + ``_parse_first_line`` — strict first-token parser.

Cross-shard contracts consumed (imported lazily so this module loads on a
base branch that hasn't yet merged S2/S4/S5/S6/S8):
    S2  — ``ApplyResult`` / ``ApplyContext`` / ``Status``.
    S4  — ``session()`` context manager.
    S5  — ``DedupDB.record`` + ``.was_applied`` + ``review_pending`` schema.
    S6  — ``load_state(ats, user)``.
    S8  — ``GreenhouseAdapter.apply(page, ctx)``.
    S11 — ``@navigation_retry`` (applied to Gmail extension methods in
          ``src/gmail/client.py``, not here).

Landmine discipline:
    L6  — every ``datetime`` write goes through ``datetime.now(timezone.utc)``;
          the deprecated naive UTC-now API is NEVER called (this file's
          own suite has an in-tree grep guard).
    L7  — no candidate email, phone, or answer value is ever rendered into a
          review-email body or a structlog event; only structural metadata
          (review_id, ats, thread_id, first_sent_at, resolved_at, counts).
    L12 — the adapter used inside ``execute_confirmed_submit`` is passed by
          the caller (poller) — this module does NOT hold a class-object
          dispatch table.
    L13 — ``execute_confirmed_submit`` never retries submit; the adapter's
          own ``@submit_no_retry`` marker holds the invariant, and this
          layer NEVER wraps ``adapter.apply`` in a retry loop.
    L14 — the label prefix is read from ``config[apply][gmail_label_prefix]``
          in ONE helper (``_label_names``); the fully-qualified nested-label
          strings are never composed anywhere else in this module.

Deviations from spec (documented; the parent-agent's task briefing invited
these):
    D1. ``ensure_labels`` gains a ``config`` parameter (spec §Contracts-produced
        listed the signature as ``ensure_labels(gmail)``, but L14 requires the
        prefix to come from config; carrying config in is the only sane way to
        satisfy L14 in a single helper).
    D2. ``stage_review`` gains a ``filled_count`` keyword-only argument
        (spec §Acceptance 3 requires the count in the body but the frozen
        ``ApplyResult`` does not carry it; the caller (S8) supplies it at
        stage-time).
    D3. ``poll_pending_reviews`` gains an ``adapter`` parameter (spec
        §Contracts-produced omitted it, but ``execute_confirmed_submit``
        needs an adapter and the poller is the only call-site — L12 forbids
        us from resolving it from a class-object table).
    D4. ``execute_confirmed_submit`` gains three keyword-only injectors —
        ``session_ctx``, ``load_state_fn``, ``dedup_db`` — that default to
        the real S4/S6/S5 collaborators. Tests inject mocks; production
        code uses the defaults. This is a strict test-observability win
        and preserves the spec's return-type contract.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator
from uuid import UUID

import structlog

if TYPE_CHECKING:  # pragma: no cover
    # These imports are for type-checking only — they must not run at import
    # time so the module remains importable on a base branch that lacks
    # sibling shards (S2/S4/S5/S6/S8).
    from .state_store import ReviewStore
    from .types import ApplyContext, ApplyResult


log = structlog.get_logger()


# ─────────────────────────────────────────────────────────
# Constants (L14 — prefix comes from config, NEVER hardcoded here)
# ─────────────────────────────────────────────────────────

_LABEL_SUFFIXES: tuple[str, ...] = ("pending", "submitted", "declined")

_AMBIGUOUS_REPLY = (
    "Please reply YES or NO on the first line — I only read the first token."
)

_REPING_BODY = (
    "Still awaiting YES/NO — will auto-decline at 72h from initial send."
)

# Trailing-punctuation set stripped from the first token before matching.
_STRIP_TRAILING_PUNCT = ".!,?;:"


# ─────────────────────────────────────────────────────────
# Decision dataclass
# ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decision:
    """Resolved outcome of one review-loop tick per row.

    ``status`` is one of the S2 ``Status`` literal values —
    ``"submitted" | "declined" | "auto_declined" | "review_required"``
    (``review_required`` used on the ambiguous branch: the row stays in
    pending, no label move, no state advance).
    """

    review_id: str
    status: str
    apply_url: str
    ats: str
    company: str
    role_title: str
    applicant: str
    thread_id: str


# ─────────────────────────────────────────────────────────
# uuid7 (RFC 9562) — zero-new-deps local impl (Ben preference)
# ─────────────────────────────────────────────────────────


def _uuid7() -> str:
    """Return a fresh RFC 9562 uuid7 as a canonical string.

    Layout (128 bits):
        bits 127..80 — 48-bit unix_ts_ms.
        bits 79..76  — 4-bit version = 0b0111 (7).
        bits 75..64  — 12-bit rand_a.
        bits 63..62  — 2-bit variant = 0b10 (RFC 4122).
        bits 61..0   — 62-bit rand_b.

    Time source is ``time.time_ns() // 1_000_000`` (monotonic within a ms,
    unique-enough given 74 bits of randomness per invocation). This impl is
    good until 10889 CE, which is more than enough for a Phase 3 MVP.
    """
    ts_ms = (time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand & 0xFFF
    rand_b = (rand >> 12) & ((1 << 62) - 1)
    uuid_int = (
        (ts_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return str(UUID(int=uuid_int))


# ─────────────────────────────────────────────────────────
# Parser primitives
# ─────────────────────────────────────────────────────────

_QUOTE_RE = re.compile(r"^\s*>")


def _strip_quoted(body: str) -> str:
    """Return ``body`` with every quoted line (matching ``^\\s*>``) removed.

    Each line is inspected independently; leading whitespace before the ``>``
    is tolerated (Gmail sometimes indents quoted blocks under a signature).
    """
    if not body:
        return ""
    kept = [ln for ln in body.splitlines() if not _QUOTE_RE.match(ln)]
    return "\n".join(kept)


def _parse_first_line(first_line: str) -> str:
    """Strict first-token whole-word case-insensitive YES/NO parser.

    Returns ``"YES"`` | ``"NO"`` | ``"AMBIGUOUS"``.

    Rules (Ben Q4):
        - Split ``first_line`` on whitespace; take the first token.
        - Strip trailing punctuation ``.!,?;:`` from the token.
        - Uppercase.
        - Match against exactly ``"YES"`` or ``"NO"`` (whole-word).

    ``"Yeah"``, ``"Y"``, ``"maybe"``, ``""``, ``"submit please"`` → AMBIGUOUS.
    ``"YES please"``, ``"YES!"``, ``"YES."``, ``"Yes, submit"`` → YES.
    ``"NO thanks"``, ``"No way"``, ``"NO."`` → NO.
    """
    if not first_line:
        return "AMBIGUOUS"
    tokens = first_line.strip().split()
    if not tokens:
        return "AMBIGUOUS"
    token = tokens[0].rstrip(_STRIP_TRAILING_PUNCT).upper()
    if token == "YES":
        return "YES"
    if token == "NO":
        return "NO"
    return "AMBIGUOUS"


def _first_non_empty_line(body: str) -> str:
    """Extract the first non-empty stripped line of ``body`` after removing
    all quoted lines. Returns ``""`` if the whole body is quoted or blank."""
    stripped_body = _strip_quoted(body)
    for line in stripped_body.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


# ─────────────────────────────────────────────────────────
# Config helpers (L14 — single source of truth for the prefix)
# ─────────────────────────────────────────────────────────


def _label_names(config: dict) -> dict[str, str]:
    """Return the fully-qualified nested label names, keyed by short suffix.

    Reads ``config["apply"]["gmail_label_prefix"]`` — the ONLY place in this
    module that composes the full label string. Every other function calls
    this helper. L14 satisfied.
    """
    prefix = config["apply"]["gmail_label_prefix"]
    return {suffix: f"{prefix}/{suffix}" for suffix in _LABEL_SUFFIXES}


def _now_iso() -> str:
    """ISO-8601 UTC ``now``. Central helper so L6 audits stay one grep."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────
# Label CRUD + boot
# ─────────────────────────────────────────────────────────


def ensure_labels(gmail: Any, config: dict) -> dict[str, str]:
    """Ensure the three nested labels exist and return their IDs by short name.

    Idempotent by delegation: ``gmail.get_or_create_label(name)`` is the
    contract that never creates a duplicate label. The dict returned is
    keyed by short suffix (``pending``, ``submitted``, ``declined``); the
    values are Gmail label IDs (opaque strings).
    """
    names = _label_names(config)
    return {suffix: gmail.get_or_create_label(full) for suffix, full in names.items()}


# ─────────────────────────────────────────────────────────
# Review email body (L7 — no PII)
# ─────────────────────────────────────────────────────────


def _render_review_email(
    *,
    company: str,
    role_title: str,
    apply_url: str,
    ats: str,
    review_id: str,
    filled_count: int,
) -> tuple[str, str]:
    """Return ``(subject, body)``. Body contains ONLY structural metadata —
    no candidate email, phone, resume path, or answer values (L7)."""
    subject = f"[hiring-agent] Application to {company} — {role_title} [review_id={review_id}]"
    body = (
        f"Application to {company} — {role_title} [review_id={review_id}]\n"
        "\n"
        f"apply_url: {apply_url}\n"
        f"ats: {ats}\n"
        f"filled fields: {filled_count}\n"
        "\n"
        "Reply YES to submit, NO to skip. Only your first line is read.\n"
        "Confirmation screenshot attached inline.\n"
    )
    return subject, body


# ─────────────────────────────────────────────────────────
# stage_review
# ─────────────────────────────────────────────────────────


def stage_review(
    result: "ApplyResult",
    ctx: "ApplyContext",
    gmail: Any,
    store: "ReviewStore",
    *,
    filled_count: int = 0,
) -> str:
    """Stage a review: send the email, insert a ``review_pending`` row with
    the resulting Gmail thread id, and return the ``review_id`` (uuid7).

    ``filled_count`` is passed by the caller (S8's adapter) because the
    frozen ``ApplyResult`` does not carry it. Absent counts default to 0 —
    the body still renders; only the numeric drops to zero.

    H5 fix: ``fast_path_recipient`` is resolved via the same ``env:``-aware
    resolver S13's notify.py uses. The shipped default ``env:MY_EMAIL`` was
    previously sent verbatim to Gmail as a literal recipient string, which
    the API rejected with an invalid-address error AFTER the row had been
    inserted — leaving a row with a NULL thread id that could never be
    pinged. The fix also flips the ordering: send FIRST, insert row with the
    thread id AFTER, so a send failure leaves no orphan pending row.

    H4/M1 fix: ``resume_path``, ``cover_letter_path``, and ``applicant`` are
    persisted onto the row so the YES-branch re-run can hydrate a real
    _AutoModeCtx (not the previous hardcoded None trio).
    """
    from src.apply.notify import _resolve_recipient  # H5: reuse resolver

    review_id = _uuid7()
    first_sent_at = _now_iso()

    # H5/M3 shape reconciliation: ctx.config may be the wrapped `{"apply": ...}`
    # dict (test-suite convention) OR the inner apply dict (seam convention).
    # ensure_labels + _resolve_recipient both read the WRAPPED shape, so we
    # normalize here and pass the wrapped form downstream.
    _cfg_in = ctx.config if isinstance(ctx.config, dict) else {}
    wrapped_config = _cfg_in if "apply" in _cfg_in else {"apply": _cfg_in}
    label_ids = ensure_labels(gmail, wrapped_config)

    company = ctx.job.get("company", "")
    role_title = ctx.job.get("role_title") or ctx.job.get("title") or ""
    apply_url = result.apply_url or ctx.job.get("apply_url", "")
    ats = result.ats or ctx.job.get("ats", "")
    job_url = ctx.job.get("job_url", apply_url)

    subject, body = _render_review_email(
        company=company,
        role_title=role_title,
        apply_url=apply_url,
        ats=ats,
        review_id=review_id,
        filled_count=filled_count,
    )

    screenshot = result.confirmation_screenshot

    # H5: resolve `env:MY_EMAIL` (and any other `env:*` value) via the same
    # helper notify.py uses. Refuses to send when the resolver returns None
    # so no orphan row lands in the DB with a NULL thread id.
    to = _resolve_recipient(wrapped_config)
    if not to:
        log.warning(
            "apply.review.recipient_unresolved",
            ats=ats,
            review_id=review_id,
        )
        raise ValueError(
            "stage_review: fast_path_recipient is unresolved — cannot send "
            "review email. Set MY_EMAIL or a literal apply.fast_path_recipient."
        )

    # Send FIRST so a Gmail-side failure aborts before we touch the DB.
    attachments = [screenshot] if screenshot else []
    msg_id, thread_id = gmail.send_with_labels(
        subject=subject,
        body=body,
        to=to,
        labels=[label_ids["pending"]],
        attachments=attachments,
    )

    # Then insert the row with a live thread_id (post-review addition:
    # resume/cover paths + applicant persist so the YES branch can hydrate).
    # H4/M1: pull resume/cover paths off ctx; pull applicant off ctx or
    # config (falling back to _safe_getuser to unify the key convention
    # with bootstrap.py; SD2 fix wraps `getpass.getuser` in a try/except).
    resume_path = getattr(ctx, "resume_path", None)
    cover_letter_path = getattr(ctx, "cover_letter_path", None)
    applicant = (
        getattr(ctx, "applicant", None)
        or wrapped_config.get("apply", {}).get("user")
        or _safe_getuser()
    )

    store.insert(
        {
            "review_id": review_id,
            "job_url": job_url,
            "apply_url": apply_url,
            "company": company,
            "role_title": role_title,
            "ats": ats,
            "filled_at": first_sent_at,
            "screenshot_path": str(screenshot) if screenshot else "",
            "trace_path": (
                str(getattr(result, "trace_path", None))
                if getattr(result, "trace_path", None)
                else None
            ),
            "first_sent_at": first_sent_at,
            "last_repinged_at": None,
            "repings_sent": 0,
            "gmail_thread_id": thread_id,
            "resolution": None,
            "resolved_at": None,
            # H4/M1 additions (see review_pending migration 002).
            "resume_path": str(resume_path) if resume_path else None,
            "cover_letter_path": str(cover_letter_path) if cover_letter_path else None,
            "applicant": applicant,
            # SE3 addition (Phase 1 xhigh): store the review email's OWN msg_id
            # so poll_pending_reviews can exact-match filter it out of the
            # thread, replacing the fragile body-prefix + In-Reply-To heuristic
            # in `_is_review_own_message`.
            "initial_msg_id": msg_id,
        }
    )

    log.info(
        "apply.review.staged",
        review_id=review_id,
        ats=ats,
        thread_id=thread_id,
        first_sent_at=first_sent_at,
        filled_count=filled_count,
    )
    return review_id


# ─────────────────────────────────────────────────────────
# execute_confirmed_submit
# ─────────────────────────────────────────────────────────


def _fake_apply_result(status: str, **fields) -> Any:
    """Build an ``ApplyResult`` when the S2 shard is available; fall back to a
    duck-typed stand-in when it isn't (base branch import safety).

    The stand-in has the same attribute surface the downstream S5 dedup call
    and the poll-loop return path read from — status/ats/apply_url/etc.
    """
    try:
        from .types import ApplyResult
        return ApplyResult(status=status, **fields)
    except Exception:  # pragma: no cover — base-branch fallback
        class _Result:
            pass
        r = _Result()
        r.status = status
        for k, v in fields.items():
            setattr(r, k, v)
        for k in (
            "ats",
            "apply_url",
            "application_id",
            "confirmation_screenshot",
            "reason",
            "human_review_url",
            "submitted_at",
            "trace_path",
            "review_id",
        ):
            if not hasattr(r, k):
                setattr(r, k, None)
        return r


def _default_session_ctx() -> Callable[..., Any]:
    """Lazy import of S4's ``session`` context manager."""
    from src.browser.session import session as _s  # noqa: E402
    return _s


def _default_load_state() -> Callable[[str, str], dict | None]:
    from src.apply.credentials import load_state as _l  # noqa: E402
    return _l


def _default_dedup_db(config: dict) -> Any:
    # Anchor at repo root — prevents CWD split-brain DBs across invocation
    # sites (cron w/ a different CWD vs. manual repo-root runs).
    from src.apply.dedup import DedupDB, _resolve_db_path  # noqa: E402
    return DedupDB(_resolve_db_path(config))


def execute_confirmed_submit(
    decision: Decision,
    adapter: Any,
    config: dict,
    *,
    session_ctx: Callable[..., Any] | None = None,
    load_state_fn: Callable[[str, str], dict | None] | None = None,
    dedup_db: Any | None = None,
    resume_path: "Path | None" = None,
    cover_letter_path: "Path | None" = None,
) -> Any:
    """Re-open the browser via S4, re-run the adapter, and record dedup ONLY
    on DOM-verified confirmation.

    Never retries submit (L13). The adapter's own ``@submit_no_retry`` marker
    holds the invariant end-to-end; this layer calls ``adapter.apply`` exactly
    ONCE per invocation, and NEVER wraps it in a retry loop.

    L5 discipline (browser cleanup): the S4 ``session()`` ctx manager owns
    the ``browser + context + page`` teardown in a nested try/finally — we
    just enter/exit it cleanly.

    Idempotency: ``DedupDB.record`` may raise ``sqlite3.IntegrityError`` on
    a UNIQUE(company, ats_domain, ats_job_id) collision — which happens on
    a replay after a prior successful confirm-submit. Catch it and return
    ``ApplyResult(status="already_applied")``.
    """
    if session_ctx is None:
        session_ctx = _default_session_ctx()
    if load_state_fn is None:
        load_state_fn = _default_load_state()
    if dedup_db is None:
        dedup_db = _default_dedup_db(config)

    # Belt-and-suspenders: check dedup BEFORE re-running the adapter. If a
    # background task recorded this app during the review window, short-circuit.
    #
    # xhigh-H7/H13: pass REAL ats_domain + ats_job_id extracted from
    # decision.apply_url so the query matches the (ats_domain, ats_job_id)
    # UNIQUE index shape, NOT the fallback job_url-only branch which was a
    # cross-user leak (applicant A's YES on the same job_url matched
    # applicant B's row). Also filter by applicant to close the same leak
    # even when the pair is known.
    from src.apply.dedup import _extract_ats_domain, _extract_ats_job_id  # noqa: E402
    _ats_domain = _extract_ats_domain(decision.apply_url)
    _ats_job_id = _extract_ats_job_id(decision.apply_url)
    try:
        already = dedup_db.was_applied(
            company=decision.company,
            ats_domain=_ats_domain,
            ats_job_id=_ats_job_id,
            job_url=decision.apply_url,
            applicant=decision.applicant or None,
        )
    except TypeError:
        # Support DedupDB.was_applied signatures that differ slightly
        # (e.g. test doubles without the applicant kwarg).
        try:
            already = dedup_db.was_applied(
                company=decision.company,
                ats_domain=_ats_domain,
                ats_job_id=_ats_job_id,
                job_url=decision.apply_url,
            )
        except TypeError:
            already = dedup_db.was_applied(decision.apply_url)
    if already:
        log.info(
            "apply.review.already_recorded",
            review_id=decision.review_id,
            reason="was_applied_precheck",
        )
        return _fake_apply_result(
            "already_applied",
            ats=decision.ats,
            apply_url=decision.apply_url,
        )

    # Load storage_state; write to a temp file S4 can consume; delete post-run.
    #
    # M2 fix: bootstrap.wrap_state writes a `{"state": <playwright_state>,
    # "last_verified": ..., "user": ...}` envelope. Playwright's
    # ``browser.new_context(storage_state=<path>)`` expects the UNWRAPPED
    # inner dict (top-level `cookies` + `origins` keys). Previously we
    # json-dumped the envelope verbatim, and Playwright either restored zero
    # cookies or raised — the bootstrapped session was effectively unused.
    state = load_state_fn(decision.ats, decision.applicant) if load_state_fn else None
    if state is not None:
        # Envelope form → unwrap; already-flat state → pass through.
        #
        # SD5 fix (Phase 1 xhigh): on unwrap failure we DROP the state
        # entirely (state = None) rather than falling through with the
        # wrapped envelope still bound — the pre-fix `state` variable
        # remained the wrapper and was verbatim json.dump'd to the temp
        # file, reintroducing the exact M2 bug we're supposed to fix.
        if isinstance(state, dict) and "state" in state and "last_verified" in state:
            try:
                from src.apply.bootstrap import unwrap_state  # noqa: E402
                inner, _lv, _u = unwrap_state(state)
                state = inner
            except Exception:  # noqa: BLE001 — malformed envelope
                log.warning(
                    "apply.review.storage_state_unwrap_failed",
                    review_id=decision.review_id,
                )
                state = None
    tmp_path: Path | None = None
    if state:
        fd, name = tempfile.mkstemp(suffix=".storage_state.json")
        os.close(fd)
        tmp_path = Path(name)
        tmp_path.write_text(json.dumps(state))
        os.chmod(tmp_path, 0o600)

    result: Any
    try:
        with session_ctx(storage_state_path=tmp_path, headless=True) as session_yield:
            # S4 yields ``(page, trace_path)``; be lenient for callers that
            # yield just ``page`` (test fakes).
            if isinstance(session_yield, tuple):
                page = session_yield[0]
            else:  # pragma: no cover — defensive
                page = session_yield
            try:
                page.goto(decision.apply_url)
            except Exception:
                # goto errors are the adapter's problem — but log-and-fall-through
                # so we get an ``ApplyResult(failed)`` instead of a bare exception.
                pass
            # NEVER retry adapter.apply (L13). Called exactly once.
            # H4/M3: pass persisted resume/cover paths + unwrapped config so
            # the adapter can actually complete the upload and honor rate limits.
            result = adapter.apply(
                page,
                _AutoModeCtx(
                    decision,
                    config,
                    resume_path=resume_path,
                    cover_letter_path=cover_letter_path,
                ),
            )
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    # Only record on DOM-verified confirmation (S8 owns the confirmation
    # marker; we trust its status). Never submitted → never touch dedup DB.
    # Lazy-import AlreadyAppliedError so the branch remains importable on
    # branches that lack the S5 module.
    from src.apply.dedup import AlreadyAppliedError  # noqa: E402
    if getattr(result, "status", None) == "submitted":
        try:
            # H6 fix: role_title is a required kwarg on DedupDB.record.
            # Missing it crashed the YES branch with TypeError.
            dedup_db.record(
                result,
                applicant=decision.applicant,
                company=decision.company,
                role_title=decision.role_title,
                job_url=decision.apply_url,
            )
        except (sqlite3.IntegrityError, AlreadyAppliedError):
            # Idempotent replay path. Not an error — a rerun of the same review.
            # H6 (post-review) fix: DedupDB.record catches sqlite3.IntegrityError
            # and re-raises AlreadyAppliedError, so the plain
            # `except sqlite3.IntegrityError` was dead code — a real replay
            # would have propagated an uncaught AlreadyAppliedError into the
            # seam's poll_pending_reviews try/except and lost the whole tick.
            log.info(
                "apply.review.already_recorded",
                review_id=decision.review_id,
                reason="integrity_error_on_record",
            )
            return _fake_apply_result(
                "already_applied",
                ats=decision.ats,
                apply_url=decision.apply_url,
            )
        except Exception as exc:  # noqa: BLE001
            # xhigh-BLOCKING: record failed for a non-uniqueness reason
            # (locked DB, disk I/O, permissions, etc.). The ATS DID accept
            # the submission (status=='submitted' proved by the adapter's
            # DOM-verified confirm). Escalate to submitted_unrecorded so
            # the YES-branch caller (_handle_yes) marks the review row
            # resolved (not stuck pending → auto_decline for a real submit)
            # and the digest bucket surfaces the double-submit risk.
            log.error(
                "apply.review.record_failed.escalated",
                review_id=decision.review_id,
                exc_type=type(exc).__name__,
            )
            return _fake_apply_result(
                "submitted_unrecorded",
                ats=decision.ats,
                apply_url=decision.apply_url,
                application_id=getattr(result, "application_id", None),
                confirmation_screenshot=getattr(result, "confirmation_screenshot", None),
                submitted_at=getattr(result, "submitted_at", None),
                trace_path=getattr(result, "trace_path", None),
                reason=f"record_failed: {type(exc).__name__}",
            )
    else:
        log.warning(
            "apply.review.submit_failed",
            review_id=decision.review_id,
            status=getattr(result, "status", None),
            reason=getattr(result, "reason", None),
        )
    return result


class _AutoModeCtx:
    """Minimal ``ApplyContext``-shaped stand-in for the confirmed-submit re-run.

    Guarantees the attributes the S8 adapter contract reads —
    ``mode``, ``dry_run``, ``config``, ``applicant``, ``job``, ``profile``,
    ``resume_path``, ``cover_letter_path``. ``profile`` is lazy-loaded from
    ``config["apply"]["profile_path"]`` via S1's ``CandidateProfile.load``
    (imported lazily so this module remains importable on branches that
    haven't merged S1); if S1 is absent, ``profile`` stays None and the
    adapter's own ``load_profile`` fallback kicks in.

    H4/M1 fix: ``resume_path`` + ``cover_letter_path`` now hydrate from the
    stored review_pending row (previously hardcoded ``None`` — which caused
    every YES-confirmed re-submit to fail ``no_resume_available``).

    M3 fix: ``config`` is the UNWRAPPED inner apply-config (previously was
    the wrapped ``{"apply": ...}`` dict). The greenhouse adapter reads
    ``ctx.config.get("rate_limit_per_ats_per_day")`` etc. off the inner
    dict, so passing the wrapper silently ignored the caps.
    """

    __slots__ = (
        "mode",
        "dry_run",
        "config",
        "applicant",
        "job",
        "profile",
        "resume_path",
        "cover_letter_path",
        "dedup",
        "captcha_detector",
    )

    def __init__(
        self,
        decision: Decision,
        config: dict,
        *,
        resume_path: "Path | None" = None,
        cover_letter_path: "Path | None" = None,
    ):
        # M3: hand the adapter the INNER apply-config dict, unwrapping the
        # ``{"apply": ...}`` envelope if present (defensive — some callers
        # already pass the inner dict; keep the wrapper case working too).
        inner = config.get("apply", config) if isinstance(config, dict) else {}
        if not isinstance(inner, dict):
            inner = {}
        self.mode = "auto"
        self.dry_run = inner.get("dry_run", False)
        self.config = inner
        self.applicant = decision.applicant
        self.job = {
            "company": decision.company,
            "role_title": decision.role_title,
            "title": decision.role_title,
            "apply_url": decision.apply_url,
            "job_url": decision.apply_url,
            "ats": decision.ats,
        }
        # H4: paths flow in from the caller (execute_confirmed_submit reads
        # them off the review_pending row).
        self.resume_path = resume_path
        self.cover_letter_path = cover_letter_path
        # Profile still lazy-loaded from `profile_path` inside the config —
        # accept either wrapped or unwrapped (same helper below).
        self.profile = _load_profile_or_none(config if "apply" in (config or {}) else {"apply": inner})
        # Adapters read ctx.dedup / ctx.captcha_detector — the ReviewStore
        # doesn't have a natural place to plumb these on the YES branch, so
        # they stay None and the adapter's `getattr(ctx, ..., None)`
        # fallbacks apply (dedup gating still fires via the precheck above).
        self.dedup = None
        self.captcha_detector = None


def _load_profile_or_none(config: dict) -> Any:
    """Lazy-load S1's ``CandidateProfile`` from ``config[apply][profile_path]``.

    Returns ``None`` if S1 isn't available on this branch or if the config
    key is missing — the adapter's own fallback handles that case.
    """
    try:
        from src.apply.profile import CandidateProfile  # noqa: E402
    except Exception:
        return None
    path = config.get("apply", {}).get("profile_path")
    if not path:
        return None
    try:
        return CandidateProfile.load(path)
    except Exception as e:  # pragma: no cover — merge-time integration path
        log.warning(
            "apply.review.profile_load_failed",
            error_type=type(e).__name__,
        )
        return None


# ─────────────────────────────────────────────────────────
# poll_pending_reviews
# ─────────────────────────────────────────────────────────


def _extract_thread_body(msg: dict) -> str:
    """Pull the human body out of the message dict shape returned by
    ``GmailClient.search``. Tolerant of two field names: ``body_text`` (S12's
    contract) and ``text`` (existing ``find_unprocessed_alert`` shape)."""
    return msg.get("body_text") or msg.get("text") or msg.get("body") or ""


def _extract_thread_id(msg: dict) -> str | None:
    return msg.get("thread_id") or msg.get("threadId")


def _extract_msg_id(msg: dict) -> str | None:
    return msg.get("id") or msg.get("msg_id")


def _is_review_own_message(msg: dict, *, own_msg_ids: set[str] | None = None) -> bool:
    """H3: return True when ``msg`` is the review email itself, not a reply.

    SE3 fix (Phase 1 xhigh): now takes the SET of msg_ids we sent from
    stage_review (persisted per row as `initial_msg_id`) and does an
    EXACT match instead of the pre-fix body-prefix + In-Reply-To heuristic.
    The old heuristic silently dropped legitimate operator YES replies
    whose body top-quoted the original text (iOS Mail, Outlook top-quote,
    etc.). The exact-msg-id match has zero false positives and zero false
    negatives on the self-vs-reply question.

    ``own_msg_ids`` is optional so a caller with an incomplete view
    (e.g. a test fake without the persisted anchor) can still opt into
    the older fallback heuristics.
    """
    # Explicit override — used by test fakes to pre-tag their sent messages.
    if msg.get("is_own") or msg.get("own"):
        return True
    # SE3 primary path: exact msg_id match against the persisted anchor set.
    if own_msg_ids:
        mid = msg.get("id") or msg.get("msg_id")
        if mid and mid in own_msg_ids:
            return True
    return False


def _msg_sort_key(msg: dict) -> tuple:
    """Return a sort key for latest-wins thread selection.

    Prefers ``internal_date`` (Gmail's ms-precision int) then falls back to
    an integer id tiebreaker. All missing → ``(0, 0, "")`` so an id-less/
    date-less message loses to any other.

    SD4 fix (Phase 1 xhigh): the pre-fix tiebreaker used a raw string
    compare on the id (``str(msg.get("id"))``), so 'MSG_10' sorted BEFORE
    'MSG_2' lexicographically — the poller silently picked the older
    reply. Now we extract the trailing integer digits so numeric ids sort
    correctly, and fall back to string comparison last.
    """
    idate = msg.get("internal_date") or msg.get("internalDate") or 0
    try:
        idate = int(idate)
    except (TypeError, ValueError):
        idate = 0
    raw_id = str(msg.get("id") or msg.get("msg_id") or "")
    # Extract trailing digits for numeric tiebreak (SD4).
    digits = ""
    for ch in reversed(raw_id):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    try:
        int_id = int(digits) if digits else 0
    except ValueError:
        int_id = 0
    return (idate, int_id, raw_id)


def _safe_getuser() -> str:
    """SD2 fix (Phase 1 xhigh): sandboxed hosting environments (systemd,
    Lambda, containers with stripped LOGNAME) can raise ``OSError`` /
    ``KeyError`` from ``getpass.getuser()``. Guard so stage_review never
    dies on the applicant fallback path — 'single' matches bootstrap.py's
    Q7 single-user default.
    """
    import getpass  # noqa: E402 — stdlib, lazy for import cost
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "single"


def _bare_address(raw: str | None) -> str | None:
    """Return the canonical bare-address form of ``raw``: strip display name,
    quoted comments, angle brackets — then lowercase.

    Uses ``email.utils.parseaddr`` so RFC 5322 forms (quoted display name,
    embedded angle brackets, comments) are handled correctly. Returns
    ``None`` when ``raw`` is empty/None or when parseaddr returns an empty
    address slot.
    """
    if not raw:
        return None
    from email.utils import parseaddr  # noqa: E402 — stdlib, lazy for import cost
    _display, addr = parseaddr(raw)
    if not addr:
        return None
    return addr.strip().lower() or None


def _extract_from(msg: dict) -> str | None:
    """H1: extract the ``From`` header (or its `from_addr` / `sender` alias) so
    the poller can drop messages that did NOT come from the authorized replier.

    Returns a bare address without the ``Name <addr>`` decoration when possible.
    SE5 fix (Phase 1 xhigh): RFC 5322 parsing via `email.utils.parseaddr`
    instead of the pre-fix `raw.index('<')` / `raw.index('>')` heuristic which
    misparsed quoted display names containing angle brackets.
    """
    return _bare_address(
        msg.get("from") or msg.get("from_addr") or msg.get("sender") or ""
    )


def _authorized_replier(config: dict) -> str | None:
    """H1: resolve the address whose YES/NO reply is trusted.

    Uses the same `env:`-aware helper as ``notify._resolve_recipient`` so the
    shipped default ``env:MY_EMAIL`` maps to $MY_EMAIL at call time. Returns a
    lowercased bare address, or ``None`` when unresolvable (in which case the
    caller must FAIL CLOSED — no messages authorized).
    """
    try:
        from src.apply.notify import _resolve_recipient  # noqa: E402
    except Exception:  # pragma: no cover — defensive
        return None
    val = _resolve_recipient(config)
    return _bare_address(val)


def poll_pending_reviews(
    gmail: Any,
    store: "ReviewStore",
    now: datetime,
    config: dict,
    *,
    adapter: Any = None,
) -> list[Decision]:
    """Sweep open ``review_pending`` rows; resolve each per Q4/Q5 rules.

    Returns a ``list[Decision]`` — one entry per row that resolved this tick
    (YES/NO/auto-declined). Ambiguous replies + 24h re-pings do NOT produce
    a Decision (the row stays in pending).

    ``adapter`` is required for the YES branch (calls
    ``execute_confirmed_submit(decision, adapter, config)``). The poller
    accepts a single adapter for MVP (Greenhouse-only per Q1); Phase 3.5
    will widen this to a per-ATS adapter map resolved from
    ``config["apply"]["allowed_ats"]``.
    """
    label_names = _label_names(config)
    pending_label_full = label_names["pending"]
    label_ids = ensure_labels(gmail, config)

    reping_hours = int(config["apply"].get("review_reping_hours", 24))
    timeout_hours = int(config["apply"].get("review_timeout_hours", 72))

    # H1: resolve the authorized replier ONCE per tick. Messages whose From
    # header does NOT match are ignored (not parsed as YES/NO). SE1/SE2/SE8
    # fix (Phase 1 xhigh): FAIL CLOSED — if MY_EMAIL is unresolvable or the
    # message carries no From header, drop it. The pre-fix `if sender is
    # not None and sender != authorized` guard fell OPEN on either failure
    # mode, which meant that in production (where GmailClient.search never
    # surfaced the From header) every reply passed the filter regardless
    # of who sent it.
    authorized = _authorized_replier(config)
    if authorized is None:
        log.warning(
            "apply.review.poll_fail_closed",
            reason="authorized_replier_unresolved",
        )

    # H3: identify the review email's own thread-anchor message. Under the
    # single-account default (review sent to self) the first-in-thread
    # message is the review email itself, and its body's first token is
    # "Application" → the pre-fix parser resolves AMBIGUOUS every tick and
    # never sees the real operator YES. The fix picks the LATEST non-self
    # message in each thread; we distinguish self vs. reply by (a) the
    # per-row anchor thread_id's original send msg_id (unavailable here since
    # we only stored thread_id) OR (b) In-Reply-To presence (replies carry
    # it; originals do not) OR (c) the body's first token being "Application"
    # (the review email's signature). We use (b)+(c) as fallback in
    # ``_is_review_own_message`` below.

    # Fetch all messages currently under the pending label. Filter to inbox +
    # ``newer_than:4d`` per spec §GREEN-targets — the 4-day window covers the
    # 72h auto-decline horizon with 24h of slack for tick lag. The client's
    # ``search`` returns one dict per matching message.
    query = f'label:"{pending_label_full}" in:inbox newer_than:4d'
    inbound = gmail.search(query)

    # SE3 anchor set — collect every review email's own msg_id (persisted at
    # stage time as `initial_msg_id`). H3 self-filter uses exact-match on
    # this set, replacing the fragile body-prefix + In-Reply-To heuristic.
    open_rows = store.list_open()
    own_msg_ids: set[str] = set()
    for r in open_rows:
        anchor = r.get("initial_msg_id")
        if anchor:
            own_msg_ids.add(anchor)

    # Index inbound messages by thread_id, keeping the LATEST non-self,
    # authorized reply per thread. The pre-fix code stored first-message-wins
    # which meant the review email's OWN body drove parsing (H3).
    inbound_by_thread: dict[str, dict] = {}
    for msg in inbound:
        tid = _extract_thread_id(msg)
        if not tid:
            continue

        # H1: FAIL CLOSED on both unresolved-authorized AND missing-From.
        # Only the authorized replier's messages advance to parsing; anything
        # else waits for auto-decline. This closes the SE1 production leak
        # (search() didn't surface From → sender was None → filter fell
        # through) and the SE2/SE8 unresolved-env leak (authorized was None
        # → filter block was skipped entirely).
        sender = _extract_from(msg)
        if authorized is None or sender is None or sender != authorized:
            log.info(
                "apply.review.reply_ignored_unauthorized",
                thread_id=tid,
                reason=("authorized_unresolved" if authorized is None
                        else "sender_missing" if sender is None
                        else "sender_mismatch"),
            )
            continue

        # H3: skip messages that ARE the review email itself (SE3 exact match).
        if _is_review_own_message(msg, own_msg_ids=own_msg_ids):
            continue

        # Latest-wins: prefer higher internal_date (or int-convertible id).
        existing = inbound_by_thread.get(tid)
        if existing is None or _msg_sort_key(msg) > _msg_sort_key(existing):
            inbound_by_thread[tid] = msg

    decisions: list[Decision] = []

    for row in open_rows:
        # SA5/SC4 fix (Phase 1 xhigh): per-row try/except so a Gmail failure
        # on ONE row (transient reply_to_thread 5xx, empty-thread-metadata,
        # label API error) doesn't abort the whole poll sweep and starve
        # every other pending row's YES/NO resolution this tick.
        try:
            decision = _resolve_one(
                row=row,
                inbound_by_thread=inbound_by_thread,
                gmail=gmail,
                store=store,
                now=now,
                reping_hours=reping_hours,
                timeout_hours=timeout_hours,
                label_ids=label_ids,
                adapter=adapter,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            log.warning(
                "apply.review.row_resolve_failed",
                review_id=row.get("review_id"),
                error=str(exc),
            )
            continue
        if decision is not None:
            decisions.append(decision)

    return decisions


def _row_to_decision(row: dict, *, status: str) -> Decision:
    return Decision(
        review_id=row["review_id"],
        status=status,
        apply_url=row["apply_url"],
        ats=row["ats"],
        company=row["company"],
        role_title=row["role_title"],
        applicant=row.get("applicant") or "",
        thread_id=row.get("gmail_thread_id") or "",
    )


def _resolve_one(
    *,
    row: dict,
    inbound_by_thread: dict[str, dict],
    gmail: Any,
    store: "ReviewStore",
    now: datetime,
    reping_hours: int,
    timeout_hours: int,
    label_ids: dict[str, str],
    adapter: Any,
    config: dict,
) -> Decision | None:
    review_id = row["review_id"]
    thread_id = row.get("gmail_thread_id")
    first_sent_at = _parse_iso(row["first_sent_at"])
    age = now - first_sent_at

    # 1. 72h auto-decline — highest priority. Fires even if a reply is present
    # but was never parsed as YES/NO within the window.
    if age >= timedelta(hours=timeout_hours):
        resolved_at = _iso(now)
        # xhigh-H12: guarded CAS. Only auto-decline if the row is still
        # open. If a concurrent YES/NO handler resolved it during this
        # tick, do NOT clobber with 'auto_declined'.
        won = store.mark_resolved_from_open(review_id, "auto_declined", resolved_at)
        if not won:
            log.info(
                "apply.review.auto_decline_cas_lost",
                review_id=review_id,
                reason="row already resolved (YES/NO)",
            )
            return None
        # Label move: pending → declined. Target the message id when the row
        # has a Gmail thread — a reply message id is preferred (a reply is
        # present in ``inbound_by_thread`` even for the auto-decline branch
        # when the operator replied ambiguously and let the 72h window elapse).
        # If we NEVER got a Gmail thread_id (stage_review's send failed but
        # its insert succeeded), skip label ops — a bare review_id is not a
        # valid Gmail msg_id and would 400 the API call.
        msg = inbound_by_thread.get(thread_id) if thread_id else None
        target_id = _extract_msg_id(msg) if msg else thread_id
        if target_id:
            gmail.apply_label(target_id, label_ids["declined"])
            gmail.remove_label(target_id, label_ids["pending"])
        else:
            log.warning(
                "apply.review.auto_decline_no_thread",
                review_id=review_id,
                reason="no gmail thread_id — send likely failed at stage_review",
            )
        log.info(
            "apply.review.auto_declined",
            review_id=review_id,
            first_sent_at=row["first_sent_at"],
            resolved_at=resolved_at,
        )
        return _row_to_decision(row, status="auto_declined")

    # 2. Reply present? Parse first line.
    msg = inbound_by_thread.get(thread_id) if thread_id else None
    if msg is not None:
        body = _extract_thread_body(msg)
        first = _first_non_empty_line(body)
        parsed = _parse_first_line(first)
        msg_id = _extract_msg_id(msg) or thread_id
        if parsed == "YES":
            return _handle_yes(
                row=row,
                msg_id=msg_id,
                gmail=gmail,
                store=store,
                label_ids=label_ids,
                adapter=adapter,
                config=config,
                now=now,
            )
        if parsed == "NO":
            return _handle_no(
                row=row,
                msg_id=msg_id,
                gmail=gmail,
                store=store,
                label_ids=label_ids,
                now=now,
            )
        # AMBIGUOUS — send ONE clarification per thread, then stay silent
        # until either a valid YES/NO lands or the 72h timeout fires.
        #
        # M12 fix: previously the AMBIGUOUS branch resent the clarification
        # every poll tick — for a single "maybe tomorrow" reply that's ~144
        # duplicate emails over the 72h window. The clarified_at column on
        # review_pending gates the resend.
        if row.get("clarified_at"):
            log.info(
                "apply.review.parsed_ambiguous_skipped",
                review_id=review_id,
                thread_id=thread_id,
                reason="already_clarified",
            )
            return None
        gmail.reply_to_thread(thread_id, _AMBIGUOUS_REPLY)
        try:
            store.mark_clarified(review_id, _iso(now))
        except Exception:  # noqa: BLE001 — never block on state persistence
            log.warning(
                "apply.review.mark_clarified_failed",
                review_id=review_id,
            )
        log.info(
            "apply.review.parsed_ambiguous",
            review_id=review_id,
            thread_id=thread_id,
        )
        return None

    # 3. No reply, past re-ping window, and re-ping not yet sent.
    if (
        age >= timedelta(hours=reping_hours)
        and (row.get("repings_sent") or 0) == 0
        and thread_id
    ):
        gmail.reply_to_thread(thread_id, _REPING_BODY)
        store.mark_repinged(review_id, _iso(now))
        log.info(
            "apply.review.repinged_24h",
            review_id=review_id,
            thread_id=thread_id,
        )
        return None

    # 4. Nothing to do this tick.
    return None


def _handle_yes(
    *,
    row: dict,
    msg_id: str,
    gmail: Any,
    store: "ReviewStore",
    label_ids: dict[str, str],
    adapter: Any,
    config: dict,
    now: datetime,
) -> Decision | None:
    review_id = row["review_id"]
    decision = Decision(
        review_id=review_id,
        status="submitted",  # staged; upgraded on adapter success
        apply_url=row["apply_url"],
        ats=row["ats"],
        company=row["company"],
        role_title=row["role_title"],
        applicant=row.get("applicant") or "",
        thread_id=row.get("gmail_thread_id") or "",
    )

    log.info(
        "apply.review.parsed_yes",
        review_id=review_id,
        thread_id=decision.thread_id,
    )

    # H10: atomic claim BEFORE the real ATS submit. Two concurrent pollers
    # (or a manual overlap with cron) picking up the same open row must
    # resolve to exactly one adapter.apply call, not two. try_claim is a
    # single UPDATE on a single connection — the row-change count is the
    # authoritative signal. If we lose the race, short-circuit; the winning
    # process will resolve the row.
    claimed = store.try_claim(review_id, _iso(now))
    if not claimed:
        log.info(
            "apply.review.claim_lost",
            review_id=review_id,
            reason="row already claimed or resolved by another process",
        )
        return None
    log.info("apply.review.claim_won", review_id=review_id)

    # H4: hydrate persisted resume/cover paths from the row (previously the
    # YES branch always sent None/None to the adapter → `no_resume_available`).
    resume_path_str = row.get("resume_path")
    cover_path_str = row.get("cover_letter_path")
    # iter2-H7: wrap the adapter call in try/finally so an unexpected
    # exception (browser crash, session load failure, network error outside
    # adapter.apply's try/except) doesn't leave the row stuck in
    # 'claiming' — list_open filters `resolution IS NULL` and auto_decline's
    # CAS guard uses the same filter, so a stuck 'claiming' row is
    # invisible forever. release_claim on exception restores the row to
    # open so the next tick can retry.
    try:
        result = execute_confirmed_submit(
            decision,
            adapter,
            config,
            resume_path=Path(resume_path_str) if resume_path_str else None,
            cover_letter_path=Path(cover_path_str) if cover_path_str else None,
        )
    except Exception:
        # iter2-H7: unexpected exception path — release the claim so the
        # row is retryable + not stuck-invisible. Re-raise so
        # poll_pending_reviews' per-row try/except sees the failure.
        try:
            store.release_claim(review_id)
        except Exception:  # noqa: BLE001 — never-blocking cleanup
            pass
        raise
    status = getattr(result, "status", None)
    # M4: `already_applied` from the was_applied precheck inside
    # execute_confirmed_submit means we crashed between the ATS submit
    # (dedup.record landed in applied_jobs) and mark_resolved (review_pending)
    # on a prior tick. The real application IS out at the ATS — reconcile as
    # submitted rather than leaving the row pending to be auto_declined for
    # a real submission. The `store.mark_resolved_from_claiming` call below
    # (single write path per L3) overwrites the interim 'claiming' resolution
    # with the final resolution.
    #
    # xhigh-BLOCKING/H2: include 'submitted_unrecorded' — the ATS DID accept
    # the submission, only the DB record failed. Leaving the row pending
    # would guarantee a real double-submit on the next tick (this adapter
    # re-run would fire again and hit the same posting). Distinct resolution
    # value so the digest bucket and any compliance query can tell it apart.
    if status == "submitted":
        submit_ok = True
        final_resolution = "submitted"
    elif status == "already_applied":
        submit_ok = True
        final_resolution = "submitted"  # M4: reconcile as submitted.
    elif status == "submitted_unrecorded":
        submit_ok = True
        final_resolution = "submitted_unrecorded"  # xhigh-BLOCKING/H2
    else:
        submit_ok = False
        final_resolution = None  # unused

    if submit_ok:
        resolved_at = _iso(now)
        # xhigh-H5: guarded CAS from 'claiming' → final_resolution so a
        # concurrent handler that already resolved the row (via mark_resolved
        # or auto_decline) cannot be clobbered.
        won = store.mark_resolved_from_claiming(
            review_id, final_resolution, resolved_at
        )
        if not won:
            # iter2-H4: CAS lost. Do NOT apply the 'submitted' Gmail label —
            # the row's true state is set by whichever handler won the race
            # (release_claim reset to NULL, auto_decline, concurrent NO,
            # etc.). Applying the label anyway would diverge DB from
            # Gmail's audit trail and mislead the operator. Return None so
            # the caller sees "nothing resolved this tick for this row";
            # poll_pending_reviews' aggregator treats None as skip.
            log.info(
                "apply.review.resolve_cas_lost",
                review_id=review_id,
                intended_resolution=final_resolution,
                reason="row not in 'claiming' state at CAS time",
            )
            return None
        gmail.apply_label(msg_id, label_ids["submitted"])
        gmail.remove_label(msg_id, label_ids["pending"])
        # xhigh-BLOCKING/H2: return a decision carrying the actual status so
        # the digest bucket surfaces the submitted_unrecorded warning.
        return Decision(
            review_id=review_id,
            status=status if status == "submitted_unrecorded" else "submitted",
            apply_url=row["apply_url"],
            ats=row["ats"],
            company=row["company"],
            role_title=row["role_title"],
            applicant=row.get("applicant") or "",
            thread_id=row.get("gmail_thread_id") or "",
        )

    # Submit failed — release the 'claiming' interim claim so a retry can
    # happen on the next tick. Do NOT move the label; do NOT resolve; keep
    # the row pending. Fast-path email is S13's territory. We log the
    # failure and return an unchanged pending decision so the caller sees
    # the branch was walked.
    store.release_claim(review_id)
    log.warning(
        "apply.review.submit_failed",
        review_id=review_id,
        status=status,
    )
    return Decision(
        review_id=review_id,
        status="review_required",
        apply_url=row["apply_url"],
        ats=row["ats"],
        company=row["company"],
        role_title=row["role_title"],
        applicant=row.get("applicant") or "",
        thread_id=row.get("gmail_thread_id") or "",
    )


def _handle_no(
    *,
    row: dict,
    msg_id: str,
    gmail: Any,
    store: "ReviewStore",
    label_ids: dict[str, str],
    now: datetime,
) -> Decision | None:
    review_id = row["review_id"]
    resolved_at = _iso(now)
    # xhigh-H12: guarded CAS. Only resolve if the row is still open — if a
    # concurrent YES handler already marked it 'submitted' (or the row was
    # already claimed via try_claim), do NOT clobber.
    won = store.mark_resolved_from_open(review_id, "declined", resolved_at)
    if not won:
        # iter2-H5: CAS lost. Do NOT apply the 'declined' Gmail label —
        # a concurrent YES may have already labeled the thread 'submitted',
        # and clobbering that with 'declined' would corrupt the audit trail
        # of a REAL submission. Return None so the caller sees no decision
        # this tick for this row.
        log.info(
            "apply.review.no_resolve_cas_lost",
            review_id=review_id,
            reason="row already resolved by another handler (YES/auto_decline)",
        )
        return None
    gmail.apply_label(msg_id, label_ids["declined"])
    gmail.remove_label(msg_id, label_ids["pending"])
    log.info(
        "apply.review.parsed_no",
        review_id=review_id,
        thread_id=row.get("gmail_thread_id"),
    )
    return _row_to_decision(row, status="declined")


# ─────────────────────────────────────────────────────────
# datetime helpers (L6 — every read/write is tz-aware UTC)
# ─────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    """Serialize an aware datetime to ISO-8601. Naive datetimes are rejected."""
    if dt.tzinfo is None:  # pragma: no cover — caller-side bug
        raise ValueError("naive datetime — pass an aware UTC datetime")
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp. Assumes UTC if tz is absent."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
