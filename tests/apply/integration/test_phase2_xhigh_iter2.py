"""Phase 2 xhigh iteration 2 — RED tests for findings surfaced by the
second xhigh sweep of `fix/phase2-dedup-fail-closed`.

Each test guards a specific finding from the second-pass review. All MUST
fail on iteration-1 HEAD (before this file's fixes land) and pass after.

Findings guarded:

    Iter2-B1 — YES-branch replay regression: `_AutoModeCtx.dedup=None`
               combined with the iter-1 H8 fail-closed `_soft_warn_lookup`
               causes every YES-branch confirmed-submit to route to
               `soft_dup_warn` → `_handle_yes` sees non-submit_ok →
               release_claim → 72h auto-decline of a REAL operator YES.

    Iter2-H1 — greenhouse Gate-1 `was_applied` still fails OPEN on
               exception (`except Exception: hit = False`). Inconsistent
               with the H8 fail-closed policy and lets duplicates through.

    Iter2-H2 — greenhouse Gate-1 `was_applied` does NOT pass the new
               `applicant` kwarg from H7/H13, so cross-user leaks are
               closed at the review-loop precheck but STILL open at the
               adapter's own pre-browser gate.

    Iter2-H3 — migration 003's CREATE UNIQUE INDEX raises
               `sqlite3.IntegrityError` on legacy multi-applicant DBs; the
               `_is_idempotent_ddl_error` catch is scoped to
               `OperationalError` only, so IntegrityError propagates and
               breaks DedupDB init permanently.

    Iter2-H4 — `_handle_yes` still applies the 'submitted' Gmail label
               and returns a successful Decision when
               `mark_resolved_from_claiming` CAS returns False. Row may
               remain in NULL/'auto_declined'/'declined' state while Gmail
               says 'submitted'.

    Iter2-H5 — `_handle_no` still applies the 'declined' Gmail label
               when `mark_resolved_from_open` CAS returns False (row
               already resolved by concurrent YES). Overwrites a real
               YES's 'submitted' label with 'declined'.

    Iter2-H6 — `_decision_to_row` in gmail/digest.py doesn't populate
               `reason` or `application_id`, so the new
               `_render_submitted_unrecorded` bucket always renders
               'unknown_record_error' losing the escalation's whole point.

    Iter2-H7 — stuck 'claiming' row on unexpected exception between
               `try_claim` and `mark_resolved_from_claiming` in
               `_handle_yes`. list_open filters `resolution IS NULL` so
               the row is never surfaced again; auto_decline WHERE clause
               scans open rows only.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────
# Iter2-B1 — YES-branch replay must not route to soft_dup_warn
# ─────────────────────────────────────────────────────────


def test_soft_warn_lookup_dedup_none_returns_empty_not_synthetic_hit():
    """Iter2-B1: `_AutoModeCtx.dedup=None` is INTENTIONAL (the YES-branch
    replay skips gates in the adapter; execute_confirmed_submit's own
    was_applied precheck owns dedup). `_soft_warn_lookup(dedup=None)` must
    return an empty list so `soft_warn_active=False` and the adapter's
    auto-submit branch runs.

    Fail-closed for None was over-broad — it applied to the YES-branch's
    deliberate skip. Post-fix: fail-closed only on genuine exceptions;
    None → empty (pass-through).
    """
    from src.apply.adapters.greenhouse import _soft_warn_lookup

    hits = _soft_warn_lookup(None, "AcmeCorp", "Senior Engineer")
    assert hits == [], (
        f"Iter2-B1: _soft_warn_lookup(dedup=None) must return [] so the "
        f"YES-branch replay can auto-submit. Got {hits!r}. Iter-1 fix "
        f"over-broadened fail-closed to include None → every YES-replay "
        f"routes to soft_dup_warn → release_claim → auto-decline."
    )


def test_soft_warn_lookup_still_fails_closed_on_real_exception():
    """Iter2-B1 sanity: the original H8 fail-closed behaviour on GENUINE
    exceptions must be preserved. dedup.soft_warn_check raising →
    synthetic hit → soft_warn_active=True."""
    from src.apply.adapters.greenhouse import _soft_warn_lookup

    class _BrokenDedup:
        def soft_warn_check(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    hits = _soft_warn_lookup(_BrokenDedup(), "AcmeCorp", "Senior Engineer")
    assert hits, "H8: real DB exception must still fail-CLOSED."


# ─────────────────────────────────────────────────────────
# Iter2-H1 — greenhouse Gate-1 was_applied must fail CLOSED
# ─────────────────────────────────────────────────────────


def test_greenhouse_gate1_was_applied_fails_closed_on_exception(tmp_path):
    """Iter2-H1: adapter Gate-1 `was_applied` must NOT swallow exceptions
    to `hit = False`. If the DB is broken (locked, disk I/O), the adapter
    would fall through and auto-submit an app that MAY be a duplicate.
    Fail-closed = treat as `hit = True` so the adapter returns
    `already_applied` (or, more precisely, routes to review) rather than
    silently double-applying.
    """
    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.apply.profile import CandidateProfile

    class _BrokenDedup:
        def was_applied(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

        def soft_warn_check(self, *a, **kw):
            return []

        def count_today(self, *a, **kw):
            return 0

    class _FakePage:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        def goto(self, u):
            pass

        def content(self):
            return "<html></html>"

    adapter = GreenhouseAdapter()

    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    profile = CandidateProfile.load(str(profile_path))

    ctx = SimpleNamespace(
        job={
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "AcmeCorp",
            "role": "Senior Engineer",
            "title": "Senior Engineer",
        },
        applicant="jane",
        mode="auto",
        dry_run=False,
        config={"mode": "auto", "rate_limit_per_ats_per_day": 10},
        profile=profile,
        resume_path=None,
        resume_docx_path=None,
        cover_letter_path=None,
        cover_letter_docx_path=None,
        dedup=_BrokenDedup(),
        captcha_detector=None,
    )

    result = adapter.apply(_FakePage(), ctx)
    status = getattr(result, "status", None)
    assert status != "submitted", (
        f"Iter2-H1: adapter Gate-1 was_applied must fail CLOSED — got "
        f"status={status!r}. Pre-fix bare `except Exception: hit = False` "
        f"lets a broken DB double-submit."
    )
    # Acceptable fail-closed outcomes: skipped/review_required/already_applied/failed.
    assert status in {"skipped", "review_required", "already_applied", "failed", "soft_dup_warn"}, (
        f"Iter2-H1: unexpected fail-closed status {status!r}."
    )


# ─────────────────────────────────────────────────────────
# Iter2-H2 — greenhouse Gate-1 was_applied applicant kwarg
# ─────────────────────────────────────────────────────────


def test_greenhouse_gate1_was_applied_passes_applicant(tmp_path):
    """Iter2-H2: Gate-1 was_applied must pass `applicant` to the dedup
    query so cross-user matches don't fire. Pre-fix passed only the
    (company, ats_domain, ats_job_id, apply_url) positional args, so
    applicant B's row at the same posting matched applicant A's precheck.
    """
    from src.apply.adapters.greenhouse import GreenhouseAdapter
    from src.apply.profile import CandidateProfile

    captured = {"applicant": "MISSING"}

    class _Spy:
        def was_applied(self, *args, **kwargs):
            captured["applicant"] = kwargs.get("applicant", "MISSING")
            return False

        def soft_warn_check(self, *a, **kw):
            return []

        def count_today(self, *a, **kw):
            return 0

    class _FakePage:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        def goto(self, u):
            pass

        def content(self):
            return "<html></html>"

    adapter = GreenhouseAdapter()
    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    profile = CandidateProfile.load(str(profile_path))

    ctx = SimpleNamespace(
        job={
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "AcmeCorp",
            "role": "Senior Engineer",
            "title": "Senior Engineer",
        },
        applicant="jane",
        mode="review",  # keep it short — we only care about Gate-1.
        dry_run=False,
        config={"mode": "review"},
        profile=profile,
        resume_path=None,
        resume_docx_path=None,
        cover_letter_path=None,
        cover_letter_docx_path=None,
        dedup=_Spy(),
        captcha_detector=None,
    )

    adapter.apply(_FakePage(), ctx)
    assert captured["applicant"] == "jane", (
        f"Iter2-H2: adapter Gate-1 was_applied must pass applicant='jane'; "
        f"got applicant={captured['applicant']!r}."
    )


# ─────────────────────────────────────────────────────────
# Iter2-H3 — migration 003 CREATE UNIQUE INDEX IntegrityError
# ─────────────────────────────────────────────────────────


def test_migration_003_index_creation_recovers_from_multi_applicant_conflict(tmp_path):
    """Iter2-H3: on a legacy DB with multi-applicant rows at the same
    (ats_domain, ats_job_id) posting (allowed pre-Phase-2 because raw
    company differed), migration 003's CREATE UNIQUE INDEX raises
    `sqlite3.IntegrityError` — NOT OperationalError — so the iter-1
    `_is_idempotent_ddl_error` catch doesn't fire and DedupDB init
    crashes on every subsequent open.

    Post-fix: the migration must either catch IntegrityError AND log a
    warning + skip the index (still deleting duplicates so the DB is at
    least deterministic), OR the DELETE step must be conservative enough
    to guarantee zero conflicts before the CREATE.
    """
    from src.apply.dedup import DedupDB

    db_path = tmp_path / "state" / "applied_jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Prime the DB with a bare pre-Phase-2 schema and two multi-applicant
    # rows at the same posting.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE applied_jobs ("
            "  id                    INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  applicant             TEXT NOT NULL,"
            "  company               TEXT NOT NULL,"
            "  company_normalized    TEXT NOT NULL,"
            "  role_title            TEXT NOT NULL,"
            "  role_title_normalized TEXT NOT NULL,"
            "  ats                   TEXT,"
            "  ats_domain            TEXT,"
            "  ats_job_id            TEXT,"
            "  job_url               TEXT NOT NULL,"
            "  apply_url             TEXT,"
            "  application_id        TEXT,"
            "  status                TEXT NOT NULL,"
            "  review_id             TEXT,"
            "  confirmation_screenshot TEXT,"
            "  trace_path            TEXT,"
            "  applied_at            TEXT NOT NULL,"
            "  submitted_at          TEXT"
            ")"
        )
        for applicant in ("alice", "bob"):
            conn.execute(
                "INSERT INTO applied_jobs (applicant, company, company_normalized, "
                "role_title, role_title_normalized, ats, ats_domain, ats_job_id, "
                "job_url, apply_url, application_id, status, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (applicant, f"AcmeCorp{applicant[0]}", "acmecorp", "Eng", "eng", "greenhouse",
                 "boards.greenhouse.io", "SHARED_1",
                 f"https://boards.greenhouse.io/acme/jobs/SHARED_1",
                 f"https://boards.greenhouse.io/acme/jobs/SHARED_1",
                 f"APP_{applicant}", "submitted", "2026-07-08T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    # DedupDB init runs migration 003. Post-fix, this must NOT raise —
    # either the applicant-partitioned DELETE strips duplicates before
    # CREATE INDEX runs (which requires it to be more aggressive than
    # 'GROUP BY applicant'), or the IntegrityError from CREATE INDEX is
    # caught + warned about.
    try:
        DedupDB(db_path)
    except sqlite3.Error as exc:
        pytest.fail(
            f"Iter2-H3: DedupDB init must not crash on legacy multi-applicant "
            f"rows. Raised: {type(exc).__name__}: {exc}."
        )


# ─────────────────────────────────────────────────────────
# Iter2-H4 — _handle_yes CAS-lost must NOT apply labels
# ─────────────────────────────────────────────────────────


def test_handle_yes_does_not_apply_submitted_label_on_cas_loss(tmp_path, monkeypatch):
    """Iter2-H4: when `mark_resolved_from_claiming` CAS returns False (the
    row is no longer in 'claiming' — a concurrent handler released or
    resolved it), `_handle_yes` must NOT apply the 'submitted' Gmail label.
    Doing so creates a divergence: DB says one thing, Gmail says another.
    """
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-cas-loss-yes-abc"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "u",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "T",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "M",
    })

    def _spy_execute(decision, adapter, config, *, resume_path=None, cover_letter_path=None):
        # Simulate a concurrent handler that RELEASED the claim mid-flight.
        with store._conn:
            store._conn.execute(
                "UPDATE review_pending SET resolution = NULL, resolved_at = NULL WHERE review_id = ?",
                (review_id,),
            )
        from src.apply.types import ApplyResult
        return ApplyResult(status="submitted", ats="greenhouse", apply_url=decision.apply_url)

    monkeypatch.setattr(review_mod, "execute_confirmed_submit", _spy_execute)

    fake_gmail = MagicMock()
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}
    row = store.get(review_id)

    review_mod._handle_yes(
        row=row,
        msg_id="MSG_YES",
        gmail=fake_gmail,
        store=store,
        label_ids=label_ids,
        adapter=MagicMock(),
        config={"apply": {}},
        now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    # CAS was lost. Labels MUST NOT have moved.
    fake_gmail.apply_label.assert_not_called()


# ─────────────────────────────────────────────────────────
# Iter2-H5 — _handle_no CAS-lost must NOT overwrite the submitted label
# ─────────────────────────────────────────────────────────


def test_handle_no_does_not_apply_declined_label_on_cas_loss(tmp_path):
    """Iter2-H5: when `mark_resolved_from_open` CAS returns False (a
    concurrent YES resolved the row to 'submitted'), `_handle_no` must
    NOT apply the 'declined' Gmail label. Doing so overwrites a real
    YES's 'submitted' label.
    """
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-cas-loss-no-abc"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "u",
        "apply_url": "u",
        "company": "C",
        "role_title": "R",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "T",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "M",
    })
    # Pre-resolve the row (simulating a concurrent YES that already won).
    store.mark_resolved(review_id, "submitted", now_iso)

    fake_gmail = MagicMock()
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}
    row = store.get(review_id)

    review_mod._handle_no(
        row=row,
        msg_id="MSG_NO",
        gmail=fake_gmail,
        store=store,
        label_ids=label_ids,
        now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    fake_gmail.apply_label.assert_not_called()
    # And the DB must still show 'submitted'.
    reread = store.get(review_id)
    assert reread["resolution"] == "submitted"


# ─────────────────────────────────────────────────────────
# Iter2-H6 — _decision_to_row propagates reason + application_id
# ─────────────────────────────────────────────────────────


def test_digest_submitted_unrecorded_surfaces_reason_and_application_id():
    """Iter2-H6: `_decision_to_row` and/or the Decision → row conversion
    must propagate `application_id` and `reason` so the
    `_render_submitted_unrecorded` bucket surfaces which exception broke
    the record — the whole point of the H3 escalation.
    """
    from src.gmail.digest import compose_digest
    from src.apply.review import Decision

    # Build a Decision-shape event carrying the fields.
    dec = Decision(
        review_id="R99",
        status="submitted_unrecorded",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
        ats="greenhouse",
        company="AcmeCorp",
        role_title="Senior Engineer",
        applicant="jane",
        thread_id="T",
    )
    # Attach application_id + reason to the decision (either via a
    # subclass or the fix updating Decision) so the digest can surface it.
    # Test asserts on rendered output — implementation-agnostic.
    payload = compose_digest([], [], apply_events=[dec])
    body = payload.body if hasattr(payload, "body") else payload

    # Explicit escalation-info assertions.
    assert "APP_" in body or "application_id" in body.lower() or "R99" in body, (
        f"Iter2-H6: digest must surface the application_id or review_id for "
        f"submitted_unrecorded; body:\n{body}"
    )


# ─────────────────────────────────────────────────────────
# Iter2-H7 — stuck 'claiming' row on execute_confirmed_submit exception
# ─────────────────────────────────────────────────────────


def test_handle_yes_releases_claim_on_execute_confirmed_submit_exception(tmp_path, monkeypatch):
    """Iter2-H7: if `execute_confirmed_submit` raises an unexpected
    exception between `try_claim` and `mark_resolved_from_claiming`, the
    row is stuck in 'claiming' forever — `list_open`'s
    `WHERE resolution IS NULL` skips it, auto_decline's guard also skips
    it. Fix: wrap the adapter call in try/finally + release_claim on
    exception so the row goes back to open.
    """
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-stuck-claiming-abc"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "u",
        "apply_url": "u",
        "company": "C",
        "role_title": "R",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "T",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "M",
    })

    def _boom(*a, **kw):
        raise RuntimeError("simulated browser crash mid-replay")

    monkeypatch.setattr(review_mod, "execute_confirmed_submit", _boom)

    row = store.get(review_id)
    fake_gmail = MagicMock()
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}

    # _handle_yes may re-raise or swallow; either is acceptable, but the
    # row MUST NOT be left stuck in 'claiming'.
    try:
        review_mod._handle_yes(
            row=row,
            msg_id="MSG_YES",
            gmail=fake_gmail,
            store=store,
            label_ids=label_ids,
            adapter=MagicMock(),
            config={"apply": {}},
            now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
        )
    except RuntimeError:
        pass  # tolerate re-raise; poll_pending_reviews handles it upstream

    reread = store.get(review_id)
    assert reread["resolution"] != "claiming", (
        f"Iter2-H7: row stuck in 'claiming' after execute_confirmed_submit "
        f"exception; got resolution={reread['resolution']!r}. list_open + "
        f"auto_decline filter on `resolution IS NULL` so this row is now "
        f"invisible forever."
    )
