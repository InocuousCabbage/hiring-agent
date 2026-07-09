"""Phase 2 RED integration tests — dedup integrity, fail-closed.

Findings guarded (all 8 Phase 2 items from `.agent/codebase-audit-2026-07-08.md`):

    B2  — DedupDB.record() must accept `Path` values in confirmation_screenshot /
          trace_path without raising sqlite3.ProgrammingError. Today the bind
          fails silently and no row lands in applied_jobs.
    B3  — When `record()` raises after a DOM-verified submit, the adapter must
          NOT return plain `status="submitted"` (which claims success). It must
          surface a distinct status (e.g. `submitted_unrecorded`) OR fire an
          escalation hook so the operator sees the double-submit risk.
    H7  — When DedupDB init fails at seam entry, auto-mode must refuse to
          submit. Fail-open (dedup=None → adapter proceeds → double-apply on
          any DB glitch) is unacceptable.
    H8  — The soft-dup check-site (greenhouse adapter) must key with the SAME
          normalization functions record() uses. Today the check does
          `.strip().lower()` while record stores `normalize_company` /
          `normalize_role`, so 'Stripe, Inc.' + 'Senior Data Engineer' record
          then miss on the next posting.
    H9  — The HARD UNIQUE index must not weaken on raw-company spelling
          variance. 'Acme' vs 'Acme, Inc.' at the same (ats_domain, ats_job_id)
          must be treated as the SAME posting by both `was_applied` and the
          `INSERT` constraint.
    H10 — The YES-branch submit must claim the row atomically before invoking
          the adapter. Two concurrent pollers (or a manual overlap with cron)
          must resolve to exactly one adapter.apply call, not two.
    M4  — Crash between `dedup.record()` (row in applied_jobs) and
          `store.mark_resolved(...)` (row in review_pending) must recover on
          replay by marking the review row `submitted` — NOT re-pinging and
          eventually auto_declining a real submission.
    L3  — The duplicate `review_pending` CRUD layer on DedupDB
          (`insert_review_pending`, `update_review_resolution`, etc.) must be
          removed or delegated. There must be a single write-path for the
          review store; a drifting parallel CRUD is a landmine.

Every test in this file MUST fail on main @ 0340092 (Phase 1 landing).
They go GREEN once each Phase 2 finding is addressed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


# Ensure `src` is importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────


def _apply_result_ns(
    *,
    status: str = "submitted",
    ats: str = "greenhouse",
    apply_url: str = "https://boards.greenhouse.io/acme/jobs/12345",
    application_id: str | None = "APP_1",
    confirmation_screenshot=None,
    trace_path=None,
    review_id: str | None = None,
    submitted_at: str | None = "2026-07-08T00:00:00+00:00",
    reason: str | None = None,
    human_review_url: str | None = None,
):
    """Duck-typed ApplyResult stand-in (matches the fields DedupDB.record reads)."""
    return SimpleNamespace(
        status=status,
        ats=ats,
        apply_url=apply_url,
        application_id=application_id,
        confirmation_screenshot=confirmation_screenshot,
        trace_path=trace_path,
        review_id=review_id,
        submitted_at=submitted_at,
        reason=reason,
        human_review_url=human_review_url,
    )


def _screenshot_bytes(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def _trace_bytes(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PK\x03\x04")  # ZIP signature
    return p


# ─────────────────────────────────────────────────────────
# B2 — Path→str binding fix
# ─────────────────────────────────────────────────────────


def test_b2_record_accepts_path_values_and_was_applied_hits(tmp_path):
    """B2: record() must bind Path values as strings.

    Failure mode today: sqlite3 raises `ProgrammingError: type PosixPath is
    not supported` for `confirmation_screenshot` (parameter 14) and/or
    `trace_path` (parameter 15). No row lands in applied_jobs, so the next
    `was_applied` returns False and the agent silently re-applies.

    RED: on main this raises ProgrammingError; the assert_was_applied trips
    False because no row exists.
    GREEN: record binds Path→str, row lands, was_applied returns True.
    """
    from src.apply.dedup import DedupDB

    db = DedupDB(tmp_path / "state" / "applied_jobs.db")
    screenshot = _screenshot_bytes(tmp_path / "shots" / "sub.png")
    trace = _trace_bytes(tmp_path / "traces" / "sub.zip")

    result = _apply_result_ns(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
        application_id="APP_1",
        confirmation_screenshot=screenshot,  # Path — MUST bind cleanly.
        trace_path=trace,                    # Path — MUST bind cleanly.
    )

    # Must NOT raise ProgrammingError.
    db.record(
        result,
        applicant="jane",
        company="AcmeCorp",
        role_title="Senior Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/12345",
    )

    # Row must actually land, so a follow-up run's precheck hits it.
    hit = db.was_applied(
        "AcmeCorp",
        "boards.greenhouse.io",
        "12345",
        "https://boards.greenhouse.io/acme/jobs/12345",
    )
    assert hit is True, (
        "B2: after record() the applied_jobs row must be persisted so "
        "was_applied returns True. On main this returns False because the "
        "Path bind raised ProgrammingError and the row never landed."
    )


# ─────────────────────────────────────────────────────────
# B3 — record-failure escalation
# ─────────────────────────────────────────────────────────


def test_b3_record_failure_after_dom_verified_submit_is_not_plain_submitted(tmp_path):
    """B3: when `record()` raises AFTER a DOM-verified submission, the adapter
    must NOT return `status="submitted"` unchanged. Doing so guarantees a
    silent double-apply on the next run because `was_applied` still misses.

    Contract: the returned status must be a distinct value (e.g.
    'submitted_unrecorded') OR the result must carry a non-None escalation
    signal (e.g. a `reason` mentioning the record failure and/or a durable
    marker file in the retention area). This RED test asserts one of those
    signals; the exact mechanism is fix-group's choice.

    RED today: the greenhouse adapter's blanket `except Exception` at line
    988 downgrades to `apply.dedup.record_failed` warning and still returns
    plain `submitted`. GREEN once the escalation is wired.
    """
    from src.apply.dedup import DedupDB
    from src.apply.types import ApplyResult

    class _RaisingDedup:
        """Wrapper that raises on record() but forwards other methods."""

        def __init__(self, inner: DedupDB) -> None:
            self._inner = inner
            self.record_calls = 0

        def was_applied(self, *a, **kw):
            return False

        def soft_warn_check(self, *a, **kw):
            return []

        def count_today(self, *a, **kw):
            return 0

        def record(self, *a, **kw):
            self.record_calls += 1
            raise sqlite3.OperationalError("database is locked")

    inner = DedupDB(tmp_path / "state" / "applied_jobs.db")
    dedup = _RaisingDedup(inner)

    # Simulate the exact post-submit escalation path in the greenhouse adapter.
    # After the fix, when ctx.dedup.record raises, the adapter must return a
    # result whose status is NOT plain "submitted" OR which carries an
    # escalation signal that the operator can see.
    from src.apply.adapters.greenhouse import GreenhouseAdapter

    adapter = GreenhouseAdapter()

    # Call the adapter's record-with-escalation branch directly. Fix-group
    # may expose this as a helper (`_record_or_escalate`) or inline it — either
    # way, calling adapter.apply()'s post-submit path with a raising dedup
    # must produce a NON-plain-submitted result.
    result = _apply_result_ns(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
        application_id="APP_99",
    )
    # If the adapter exposes a dedicated helper, call it. Otherwise assert
    # that the greenhouse adapter's post-record handler mutates the result
    # into a non-plain-submitted shape.
    #
    # Contract check: the fix must add either
    #   (a) a helper `_record_or_escalate(dedup, result, applicant, company,
    #       role, job_url) -> ApplyResult` that returns a status != "submitted"
    #       when record raises; OR
    #   (b) call notify.notify_record_failed(...) as an escalation hook.
    #
    # We test EITHER path — pick whichever the fix uses.
    helper = getattr(adapter, "_record_or_escalate", None)
    if helper is None:
        # Fallback: look for a module-level helper.
        from src.apply.adapters import greenhouse as _gh_mod
        helper = getattr(_gh_mod, "_record_or_escalate", None)
    assert helper is not None, (
        "B3: post-fix the adapter (or module) must expose `_record_or_escalate` "
        "so the record-failure path returns a distinct status instead of "
        "swallowing to plain 'submitted'."
    )

    out = helper(
        dedup,
        result,
        applicant="jane",
        company="AcmeCorp",
        role_title="Senior Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/99",
    )
    assert dedup.record_calls == 1, (
        "B3: record must be attempted exactly once (never retried on failure)."
    )
    status = getattr(out, "status", None)
    assert status != "submitted", (
        f"B3: record failure must NOT be returned as plain 'submitted'; "
        f"got status={status!r}. This is the silent double-apply guarantee."
    )
    # The distinct status should encode the failure — either the new
    # 'submitted_unrecorded' literal OR a reason string on the result.
    reason = getattr(out, "reason", None) or ""
    assert (
        status == "submitted_unrecorded"
        or "record" in reason.lower()
        or "unrecorded" in reason.lower()
    ), (
        f"B3: result must signal the record failure via status or reason; "
        f"got status={status!r} reason={reason!r}."
    )


# ─────────────────────────────────────────────────────────
# H7 — fail-closed on dedup init
# ─────────────────────────────────────────────────────────


def test_h7_seam_fails_closed_when_dedup_init_raises(tmp_path, monkeypatch):
    """H7: when DedupDB construction raises, auto-mode must NOT submit.

    Failure mode today: seam catches the exception, sets `ctx.dedup = None`,
    and the greenhouse adapter's blanket try/except around every gate coerces
    None-attribute errors to "not applied"/count 0/no warning — so auto mode
    submits a job that MAY already be in the DB. Combined with B3 this is a
    deterministic double-apply on any DB glitch.

    Contract: when dedup init fails and mode='auto', run_for_job must return
    a result whose status is NOT `submitted` — either `review_required`,
    `failed`, or `skipped` (with a reason mentioning dedup).
    """
    import src.apply._seam as seam_mod

    # Force DedupDB construction to fail.
    from src.apply import dedup as dedup_mod

    class _BrokenDedupDB:
        def __init__(self, *a, **kw):
            raise sqlite3.OperationalError("simulated dedup init failure")

    monkeypatch.setattr(dedup_mod, "DedupDB", _BrokenDedupDB)

    # Also patch the seam-level reference (import site).
    # `_seam.run_for_job` does `from src.apply.dedup import DedupDB as _DedupDB`
    # inside the function body — monkeypatching `dedup_mod.DedupDB` is enough
    # because the inner import re-reads the module attribute.

    # Fake gmail so stage_review's downstream deps are stubbed.
    class _FakeGmail:
        def get_or_create_label(self, name):
            return f"lbl:{name}"

        def send_with_labels(self, **kw):
            return ("mid", "tid")

    # Fake dispatcher.apply_to_job — should NOT be reached with status=submitted
    # if fail-closed works.
    called = {"apply_to_job": 0, "returned_status": None}

    def _fake_apply_to_job(job_url, ctx, config):
        called["apply_to_job"] += 1
        # Post-fix, the seam must short-circuit BEFORE calling the dispatcher
        # with an auto-mode ctx whose dedup is None — either return a synthetic
        # review_required, or force ctx.mode='review'. If the fix instead
        # lets the dispatcher run but forces review_required, the ctx here
        # will carry `mode='review'`, not `mode='auto'`.
        called["ctx_mode"] = getattr(ctx, "mode", None)
        called["ctx_dedup"] = getattr(ctx, "dedup", None)
        # Adapter would emit submitted; we return that to verify the fix
        # PREVENTS the dispatcher from ever running under auto with dedup=None.
        from src.apply.types import ApplyResult
        return ApplyResult(
            status="submitted",
            ats="greenhouse",
            apply_url=job_url,
            application_id="APP_1",
        )

    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *, job_url, ctx, config: _fake_apply_to_job(job_url, ctx, config))

    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    apply_config = {
        "enabled": True,
        "mode": "auto",  # AUTO — the dangerous branch.
        "dry_run": False,
        "allowed_ats": ["greenhouse"],
        "long_tail": "none",
        "timeout_seconds": 90,
        "navigation_retries": 2,
        "fast_path_recipient": "env:MY_EMAIL",
        "review_reping_hours": 24,
        "review_timeout_hours": 72,
        "rate_limit_per_ats_per_day": 10,
        "retention_days": 30,
        "gmail_label_prefix": "hiring-agent/apply",
        "screenshot_dir": str(tmp_path / "shots"),
        "trace_dir": str(tmp_path / "traces"),
        "storage_state_dir": str(tmp_path / "cred"),
        "dedup_db_path": str(tmp_path / "state" / "applied_jobs.db"),
        "captcha_action": "escalate",
        "captcha_transport": "browserbase",
        "profile_path": str(profile_path),
        "user": "jane",
        "browserbase": {
            "enabled": False,
            "solve_captchas": False,
            "proxies": False,
            "block_ads": True,
        },
    }
    job = {
        "url": "https://boards.greenhouse.io/acme/jobs/99999",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/99999",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/99999",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "title": "Senior Engineer",
    }

    result = seam_mod.run_for_job(
        job=job,
        jd_text="fake jd",
        lane={"name": "backend", "label": "backend"},
        resume_path=None,
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
        gmail_client=_FakeGmail(),
    )

    # Fail-closed contract: EITHER
    #   (a) run_for_job returns a non-submitted result (review_required /
    #       skipped / failed with reason mentioning dedup), OR
    #   (b) the seam forced ctx.mode='review' before dispatching, so the
    #       dispatcher was called with mode='review'.
    #
    # Both (a) and (b) preserve the "no auto-submit under broken dedup"
    # invariant. Failing today: the seam silently sets dedup=None and lets
    # the dispatcher run with mode='auto', so result.status='submitted'.
    status = getattr(result, "status", None)
    ctx_mode = called.get("ctx_mode")
    reason = getattr(result, "reason", None) or ""
    fail_closed_ok = (
        status in {"review_required", "skipped", "failed"}
        or ctx_mode == "review"
        or "dedup" in reason.lower()
    )
    assert fail_closed_ok, (
        f"H7: dedup init failure must fail CLOSED. Got status={status!r}, "
        f"ctx_mode={ctx_mode!r}, reason={reason!r}. On main this returns "
        f"'submitted' via ctx.dedup=None + swallowed adapter exceptions."
    )


# ─────────────────────────────────────────────────────────
# H8 — soft-dup check normalization
# ─────────────────────────────────────────────────────────


def test_h8_soft_dup_check_normalizes_at_greenhouse_check_site(tmp_path, monkeypatch):
    """H8: the greenhouse adapter's soft-dup check must normalize the same
    way `record()` writes. Today the check uses `.strip().lower()` but record
    stores `normalize_company` / `normalize_role`, so 'Stripe, Inc.' +
    'Senior Data Engineer' record then miss on any later 'Stripe, Inc.'
    posting → spec §13c bypassed → the auto-submit gate never fires.

    Contract: a spy that captures the args passed to soft_warn_check by the
    adapter must see normalized values (or the check must hit on the row).

    We drive the adapter's exact check-site call pattern via a targeted
    helper — the fix may expose `_soft_warn_check` on the adapter/module or
    inline the normalize calls. Either satisfies the invariant.
    """
    from src.apply.dedup import DedupDB, normalize_company, normalize_role
    from src.apply.adapters import greenhouse as gh_mod

    db = DedupDB(tmp_path / "state" / "applied_jobs.db")

    # Record with the "messy" real-world variant.
    db.record(
        _apply_result_ns(
            status="submitted",
            ats="greenhouse",
            apply_url="https://boards.greenhouse.io/stripe/jobs/999",
        ),
        applicant="jane",
        company="Stripe, Inc.",
        role_title="Senior Data Engineer",
        job_url="https://boards.greenhouse.io/stripe/jobs/999",
    )

    # Spy on soft_warn_check to capture what the adapter actually passes.
    captured: list[tuple] = []
    real_soft_warn = db.soft_warn_check

    def _spy(company_n, role_n):
        captured.append((company_n, role_n))
        return real_soft_warn(company_n, role_n)

    db.soft_warn_check = _spy  # type: ignore[assignment]

    # Fix-group must expose a helper the adapter uses (post-H8), OR verify
    # the spy sees normalized args via an integration invocation. We test
    # for the helper first (cheap check), then fall back to the outcome.
    helper = getattr(gh_mod, "_soft_warn_lookup", None)
    if helper is not None:
        hits = helper(db, "Stripe, Inc.", "Senior Data Engineer")
        assert hits, (
            "H8: helper `_soft_warn_lookup` must normalize inputs; got no hits "
            "for a company recorded moments ago under the same messy name."
        )
        # Also confirm the spy was called with normalized args (not raw).
        assert captured, "H8: helper must call soft_warn_check under the hood."
        args = captured[0]
        assert args == (
            normalize_company("Stripe, Inc."),
            normalize_role("Senior Data Engineer"),
        ), (
            f"H8: soft_warn_check must be called with normalized args; got {args!r}. "
            f"Expected {(normalize_company('Stripe, Inc.'), normalize_role('Senior Data Engineer'))!r}."
        )
    else:
        # Fallback: assert the adapter's inline check-site code passes
        # normalized args by grepping the source.
        adapter_src = Path(gh_mod.__file__).read_text()
        # After fix, the check should reference normalize_company / normalize_role.
        assert (
            "normalize_company(" in adapter_src and "normalize_role(" in adapter_src
        ), (
            "H8: greenhouse adapter must normalize company/role at the soft-warn "
            "check site (either inline via normalize_company/normalize_role, "
            "or via a `_soft_warn_lookup` helper). Neither found."
        )


# ─────────────────────────────────────────────────────────
# H9 — HARD UNIQUE index normalized on company
# ─────────────────────────────────────────────────────────


def test_h9_hard_dedup_normalized_survives_company_spelling_variance(tmp_path):
    """H9: HARD dedup must not weaken on raw-company spelling variance.

    Today the UNIQUE index is (company, ats_domain, ats_job_id) and
    `was_applied` matches raw-company equality. 'Acme' vs 'Acme, Inc.' at the
    same (ats_domain, ats_job_id) slips through both the equality check AND
    the UNIQUE constraint → same posting recorded twice.

    Contract:
        - `was_applied('Acme, Inc.', 'boards.greenhouse.io', '1', ...)` after
          recording 'Acme' at the same triple must return True.
        - A second `record()` with 'Acme, Inc.' at the same triple must raise
          `AlreadyAppliedError`.

    The fix will typically add a UNIQUE index on (ats_domain, ats_job_id)
    (dropping raw company from the key) and switch `was_applied` to match on
    that pair when both parts are non-null. Any equivalent structural fix
    that satisfies both invariants passes.
    """
    from src.apply.dedup import DedupDB, AlreadyAppliedError

    db = DedupDB(tmp_path / "state" / "applied_jobs.db")

    # First scrape: 'Acme'.
    db.record(
        _apply_result_ns(
            status="submitted",
            ats="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        applicant="jane",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    # Second scrape: 'Acme, Inc.' — same posting, different spelling.
    hit = db.was_applied(
        "Acme, Inc.",
        "boards.greenhouse.io",
        "1",
        "https://boards.greenhouse.io/acme/jobs/1",
    )
    assert hit is True, (
        "H9: was_applied must match on (ats_domain, ats_job_id) — not on raw "
        "company equality. 'Acme, Inc.' was scraped again at the same posting; "
        "the gate must fire."
    )

    # And the UNIQUE constraint must catch the double-record.
    with pytest.raises(AlreadyAppliedError):
        db.record(
            _apply_result_ns(
                status="submitted",
                ats="greenhouse",
                apply_url="https://boards.greenhouse.io/acme/jobs/1",
            ),
            applicant="jane",
            company="Acme, Inc.",
            role_title="Engineer",
            job_url="https://boards.greenhouse.io/acme/jobs/1",
        )


# ─────────────────────────────────────────────────────────
# H10 — atomic claim before YES submit
# ─────────────────────────────────────────────────────────


def test_h10_atomic_claim_prevents_concurrent_yes_double_submit(tmp_path, monkeypatch):
    """H10: two concurrent YES-branch handlers on the same review_pending row
    must produce exactly ONE adapter.apply call. The real submit happens
    BEFORE mark_resolved(review_id, 'submitted') so a check-then-act between
    separate SQLite connections is racy — manual overlap with cron double-
    submits.

    Contract: `ReviewStore` must expose an atomic `try_claim(review_id, at)
    -> bool` that sets `resolution='claiming'` iff currently NULL (single
    UPDATE, single connection), returning True on success. `_handle_yes` must
    call it before invoking execute_confirmed_submit and short-circuit on
    False.

    The RED test drives two `_handle_yes` calls interleaved with the DB in a
    state simulating a competing poller having claimed the row between the
    was_applied precheck and the adapter re-run.
    """
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-h10-test-review-id-0001-abcdef012345"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "https://boards.greenhouse.io/acme/jobs/42",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/42",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "THREAD_42",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "MSG_1",
    })

    # Post-fix, the store must expose an atomic claim helper.
    try_claim = getattr(store, "try_claim", None)
    assert try_claim is not None and callable(try_claim), (
        "H10: ReviewStore must expose `try_claim(review_id, at) -> bool` for "
        "atomic YES-branch claiming. Missing on main."
    )

    # First claim wins; second loses.
    assert try_claim(review_id, now_iso) is True, (
        "H10: first try_claim on a NULL resolution must succeed."
    )
    assert try_claim(review_id, now_iso) is False, (
        "H10: second try_claim on the same row (now non-NULL resolution) must "
        "return False — that's the atomicity guarantee."
    )

    # Now assert _handle_yes uses try_claim. Reset the row to open.
    with store._conn:
        store._conn.execute(
            "UPDATE review_pending SET resolution = NULL, resolved_at = NULL WHERE review_id = ?",
            (review_id,),
        )

    # Spy execute_confirmed_submit to count invocations.
    submits = {"count": 0}

    def _spy_execute(decision, adapter, config, *, resume_path=None, cover_letter_path=None):
        submits["count"] += 1
        # Simulate the "competing poller wins the race" scenario: BEFORE we
        # return, another process has already claimed and resolved.
        with store._conn:
            store._conn.execute(
                "UPDATE review_pending SET resolution = 'submitted', resolved_at = ? WHERE review_id = ?",
                (now_iso, review_id),
            )
        from src.apply.types import ApplyResult
        return ApplyResult(status="submitted", ats="greenhouse", apply_url=decision.apply_url)

    monkeypatch.setattr(review_mod, "execute_confirmed_submit", _spy_execute)

    open_row = store.get(review_id)
    assert open_row is not None
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}
    fake_gmail = MagicMock()

    # Two _handle_yes invocations — simulating two concurrent pollers picking
    # up the same row before either has claimed it. The atomic claim must
    # ensure exactly ONE adapter re-run happens.
    review_mod._handle_yes(
        row=open_row,
        msg_id="MSG_YES_1",
        gmail=fake_gmail,
        store=store,
        label_ids=label_ids,
        adapter=MagicMock(),
        config={"apply": {}},
        now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    # Re-read the row (its resolution was flipped inside the spy).
    open_row2 = store.get(review_id)
    review_mod._handle_yes(
        row=open_row2,
        msg_id="MSG_YES_2",
        gmail=fake_gmail,
        store=store,
        label_ids=label_ids,
        adapter=MagicMock(),
        config={"apply": {}},
        now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert submits["count"] == 1, (
        f"H10: exactly one execute_confirmed_submit call expected across two "
        f"concurrent _handle_yes invocations; got {submits['count']}. The "
        f"second call must be gated by try_claim returning False on the "
        f"already-claimed row."
    )


# ─────────────────────────────────────────────────────────
# M4 — replay-as-success reconciliation
# ─────────────────────────────────────────────────────────


def test_m4_replay_as_success_when_record_landed_but_mark_resolved_didnt(
    tmp_path, monkeypatch
):
    """M4: `dedup.record` (applied_jobs) and `store.mark_resolved`
    (review_pending) are two transactions on separate connections with a
    crash window. If the process dies between them, the next replay's
    was_applied precheck fires → `execute_confirmed_submit` returns
    `already_applied` → `_handle_yes` sees `submit_ok=False` → row stays
    pending → eventually auto_declined for an application that was actually
    submitted.

    Contract: on replay, the YES branch must reconcile — mark the review row
    'submitted' when the applied_jobs row already exists, NOT decline.
    """
    from src.apply import review as review_mod
    from src.apply.dedup import DedupDB
    from src.apply.state_store import ReviewStore

    # Pre-record the submission (simulating the pre-crash state).
    dedup_path = tmp_path / "state" / "applied_jobs.db"
    db = DedupDB(dedup_path)
    db.record(
        _apply_result_ns(
            status="submitted",
            ats="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/77",
            application_id="APP_77",
        ),
        applicant="jane",
        company="AcmeCorp",
        role_title="Senior Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/77",
    )

    # And leave the review_pending row OPEN (mark_resolved never ran).
    store = ReviewStore(dedup_path)
    review_id = "0197-m4-test-review-id-0002-abcdef012345"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "https://boards.greenhouse.io/acme/jobs/77",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/77",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "THREAD_77",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "MSG_R1",
    })

    row = store.get(review_id)
    assert row is not None

    # Fake gmail + adapter. execute_confirmed_submit should NOT be called if
    # the reconciliation short-circuits on the was_applied precheck; if it IS
    # called, its result will be 'already_applied' which the reconciliation
    # must still treat as success.
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}
    fake_gmail = MagicMock()

    review_mod._handle_yes(
        row=row,
        msg_id="MSG_YES_REPLAY",
        gmail=fake_gmail,
        store=store,
        label_ids=label_ids,
        adapter=MagicMock(),
        config={"apply": {"dedup_db_path": str(dedup_path)}},
        now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    reread = store.get(review_id)
    assert reread is not None
    assert reread["resolution"] == "submitted", (
        f"M4: replay after a crash between record() and mark_resolved must "
        f"resolve the review row as 'submitted' (the real submit that already "
        f"landed). Got resolution={reread['resolution']!r}. On main this "
        f"leaves the row pending → eventually auto_declined for a real submission."
    )


# ─────────────────────────────────────────────────────────
# L3 — dead CRUD layer removed
# ─────────────────────────────────────────────────────────


def test_l3_dedup_duplicate_review_crud_layer_removed():
    """L3: DedupDB carries a duplicate, drifting `review_pending` CRUD layer
    (`insert_review_pending`, `update_review_resolution`,
    `get_review_pending`, `list_pending_reviews`) that has zero production
    callers and disagrees with `ReviewStore.mark_resolved` on whether to set
    `resolved_at`. Two write paths for the same table is a landmine.

    Fix: delete the DedupDB methods. Callers must go through ReviewStore.
    The test asserts the methods are gone from the DedupDB surface — cheap,
    deterministic, and forces the fix-group to make the removal explicit.
    """
    from src.apply.dedup import DedupDB

    dead = [
        "insert_review_pending",
        "update_review_resolution",
        "get_review_pending",
        "list_pending_reviews",
    ]
    still_present = [name for name in dead if hasattr(DedupDB, name)]
    assert not still_present, (
        f"L3: the following duplicate review CRUD methods must be removed "
        f"from DedupDB (route through ReviewStore instead): {still_present!r}."
    )
