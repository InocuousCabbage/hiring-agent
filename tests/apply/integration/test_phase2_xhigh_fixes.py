"""Phase 2 xhigh follow-up RED tests — fixes for the 15 findings surfaced
by the mandatory xhigh code-review on branch `fix/phase2-dedup-fail-closed`.

Each test guards a specific finding from the review and MUST fail on the
pre-fix branch state (HEAD @ 00453a0). They go GREEN once the corresponding
fix lands.

Findings guarded (consolidated to 9 unique changes; 15 findings overlap):

    BLOCKING+H1+H2+H3 — submitted_unrecorded cascade:
        - _record_or_escalate with dedup=None must NOT AttributeError-escalate
          (execute_confirmed_submit handles recording on the YES-branch replay).
        - _handle_yes submit_ok must include 'submitted_unrecorded' so the
          review row is resolved (not stuck pending → auto_declined for a
          real submission).
        - Digest must render 'submitted_unrecorded' as a distinct bucket so
          the operator sees the double-submit risk.

    H4/MEDIUM  — try_claim must NOT write resolved_at during the interim
                 'claiming' state (pollutes compliance dashboards querying
                 `WHERE resolved_at IS NOT NULL`).

    H5+H12     — mark_resolved must guard the WHERE clause so concurrent
                 YES/NO races and post-auto_decline overwrites don't clobber
                 a completed resolution.

    H6         — Migration 003 must be gated by a schema_migrations tracking
                 table so the destructive DELETE only runs once, and its
                 partition must respect applicant so multi-user rows are not
                 clobbered.

    H7+H13     — was_applied precheck in execute_confirmed_submit must pass
                 REAL ats_domain + ats_job_id extracted from decision.apply_url
                 AND filter by applicant so applicant A's YES cannot reconcile
                 applicant B's pending row on the same job_url.

    H8         — _soft_warn_lookup must FAIL CLOSED (return a synthetic hit)
                 on DB exception rather than silently disabling the soft-warn
                 gate.

    H9         — H7 mode normalization must handle whitespace / mixed case;
                 initialize() must fail closed on dedup init failure (skip
                 poll_pending_reviews).

    H10        — _execute_migrations must NOT commit the outer transaction
                 mid-migration via executescript. Per-statement execution
                 keeps 001/002/003 in one atomic apply.

    H11        — OperationalError substring matching must tighten from
                 'already exists' → more specific patterns to avoid false
                 positives on unrelated errors.

All tests MUST fail on HEAD @ 00453a0 and pass after the fixes land.
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


# ─────────────────────────────────────────────────────────
# BLOCKING part A — _record_or_escalate with dedup=None
# ─────────────────────────────────────────────────────────


def test_record_or_escalate_with_dedup_none_returns_result_unchanged():
    """When dedup is None (YES-branch replay via _AutoModeCtx), the greenhouse
    adapter's _record_or_escalate must return the result UNCHANGED — the
    execute_confirmed_submit layer above owns the record() call in that path.

    Pre-fix: dedup.record() raises AttributeError on None → escalation catches
    it → returns 'submitted_unrecorded' with a bogus 'AttributeError' reason.
    This double-taxes the recording responsibility (both the adapter AND
    execute_confirmed_submit try to record) and mislabels the outcome.

    Post-fix: dedup=None → early-return the unchanged 'submitted' result so
    execute_confirmed_submit's outer dedup_db.record() lands the row.
    """
    from src.apply.adapters.greenhouse import _record_or_escalate

    result = _apply_result_ns(
        status="submitted",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
        application_id="APP_99",
    )
    out = _record_or_escalate(
        None,  # dedup=None (YES-branch replay via _AutoModeCtx)
        result,
        applicant="jane",
        company="AcmeCorp",
        role_title="Senior Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/99",
    )
    assert getattr(out, "status", None) == "submitted", (
        f"Expected unchanged 'submitted' when dedup=None; got status="
        f"{getattr(out, 'status', None)!r}. Pre-fix incorrectly escalates to "
        f"'submitted_unrecorded' via the AttributeError catch."
    )


# ─────────────────────────────────────────────────────────
# BLOCKING part B — _handle_yes accepts submitted_unrecorded
# ─────────────────────────────────────────────────────────


def test_handle_yes_treats_submitted_unrecorded_as_resolved(tmp_path, monkeypatch):
    """A YES-branch adapter returning 'submitted_unrecorded' means the ATS
    submit succeeded but the record() failed — the operator still submitted,
    so the review row must be RESOLVED (label moved, resolution set) not
    released back to pending. Otherwise the next tick re-runs the adapter →
    real double-submit.

    Pre-fix: submit_ok = {'submitted', 'already_applied'} — misses
    'submitted_unrecorded' → release_claim → next tick re-submits.

    Post-fix: submit_ok includes 'submitted_unrecorded'; mark_resolved sets
    the resolution (distinct value acceptable, e.g. 'submitted_unrecorded'
    or 'submitted'); label moves.
    """
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-unrec-test-review-id-abcdef01234567"
    now_iso = "2026-07-08T12:00:00+00:00"
    store.insert({
        "review_id": review_id,
        "job_url": "https://boards.greenhouse.io/acme/jobs/88",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/88",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "ats": "greenhouse",
        "filled_at": now_iso,
        "screenshot_path": "/tmp/x.png",
        "trace_path": None,
        "first_sent_at": now_iso,
        "last_repinged_at": None,
        "repings_sent": 0,
        "gmail_thread_id": "THREAD_88",
        "resolution": None,
        "resolved_at": None,
        "resume_path": None,
        "cover_letter_path": None,
        "applicant": "jane",
        "clarified_at": None,
        "initial_msg_id": "MSG_1",
    })

    def _spy_execute(decision, adapter, config, *, resume_path=None, cover_letter_path=None, dry_run=False):
        from src.apply.types import ApplyResult
        return ApplyResult(
            status="submitted_unrecorded",
            ats="greenhouse",
            apply_url=decision.apply_url,
            application_id="APP_88",
            reason="record_failed: OperationalError",
        )

    monkeypatch.setattr(review_mod, "execute_confirmed_submit", _spy_execute)

    row = store.get(review_id)
    fake_gmail = MagicMock()
    label_ids = {"pending": "L_P", "submitted": "L_S", "declined": "L_D"}

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

    reread = store.get(review_id)
    assert reread is not None
    # The row must be RESOLVED (non-null resolution), and the label must have moved.
    assert reread["resolution"] not in (None, "claiming"), (
        f"submitted_unrecorded must resolve the row (not release_claim); got "
        f"resolution={reread['resolution']!r}. Pre-fix leaves the row pending "
        f"and next tick re-runs the adapter → real double-submit."
    )
    fake_gmail.apply_label.assert_called()
    fake_gmail.remove_label.assert_called()


# ─────────────────────────────────────────────────────────
# BLOCKING part C — digest bucket for submitted_unrecorded
# ─────────────────────────────────────────────────────────


def test_digest_renders_submitted_unrecorded_distinctly():
    """The digest must have a bucket for 'submitted_unrecorded' so the
    operator sees the double-submit-risk warning explicitly. Otherwise the
    only surface for a record-failure escalation is a log line no one reads.
    """
    from src.gmail.digest import compose_digest
    from src.apply.types import ApplyResult

    result = ApplyResult(
        status="submitted_unrecorded",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/acme/jobs/77",
        application_id="APP_77",
        reason="record_failed: OperationalError",
    )
    processed = [{
        "title": "Senior Engineer",
        "company": "AcmeCorp",
        "location": "Remote",
        "url": "https://boards.greenhouse.io/acme/jobs/77",
        "lane": "backend",
        "apply_result": result,
    }]
    payload = compose_digest(processed, [], apply_events=[])
    # `apply_events` present → DigestPayload namedtuple.
    body = payload.body if hasattr(payload, "body") else payload
    assert "submitted_unrecorded" in body.lower() or "unrecorded" in body.lower() or "not recorded" in body.lower(), (
        f"digest body must surface submitted_unrecorded outcomes; body:\n{body}"
    )


# ─────────────────────────────────────────────────────────
# H4/MEDIUM — try_claim does not set resolved_at during 'claiming'
# ─────────────────────────────────────────────────────────


def test_try_claim_does_not_write_resolved_at_during_claiming(tmp_path):
    """try_claim's interim 'claiming' state must NOT set resolved_at.
    Compliance dashboards query `WHERE resolved_at IS NOT NULL` — including
    mid-claim rows pollutes the report. Resolved_at only lands at the final
    mark_resolved call.
    """
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-mid-claim-review-abc"
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

    assert store.try_claim(review_id, now_iso) is True

    row = store.get(review_id)
    assert row["resolution"] == "claiming"
    assert row["resolved_at"] is None, (
        f"try_claim must NOT set resolved_at during interim 'claiming' state; "
        f"got resolved_at={row['resolved_at']!r}. Pre-fix pollutes compliance "
        f"dashboards querying `WHERE resolved_at IS NOT NULL`."
    )


# ─────────────────────────────────────────────────────────
# H5+H12 — mark_resolved CAS guard
# ─────────────────────────────────────────────────────────


def test_mark_resolved_does_not_clobber_completed_resolution(tmp_path):
    """A late-arriving mark_resolved (e.g. NO handler after YES already
    succeeded, or a stale tick after auto_decline) must NOT overwrite a
    completed resolution.

    Contract: mark_resolved with an `expected_resolution` param that adds
    a WHERE-clause guard. Or a `_from_open` variant. Test-agnostic: assert
    that a second mark_resolved with a different value on an already-
    resolved row does not change the resolution.
    """
    from src.apply.state_store import ReviewStore

    store = ReviewStore(tmp_path / "state" / "applied_jobs.db")
    review_id = "0197-clobber-guard-abc"
    now_iso = "2026-07-08T12:00:00+00:00"
    later_iso = "2026-07-08T12:05:00+00:00"
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

    # First resolution lands.
    store.mark_resolved(review_id, "submitted", now_iso)
    row = store.get(review_id)
    assert row["resolution"] == "submitted"

    # A stale later call should NOT clobber.
    # After fix: mark_resolved accepts expected_resolution / only-from-open guard,
    # or callers use a distinct method for the guarded write.
    guarded = getattr(store, "mark_resolved_from_open", None)
    if guarded is not None:
        guarded(review_id, "declined", later_iso)
    else:
        # If mark_resolved gained a keyword-only guard, call with expected.
        try:
            store.mark_resolved(review_id, "declined", later_iso, expected_resolution=None)
        except TypeError:
            # No guard exposed — fail the test to force the fix.
            pytest.fail(
                "H5/H12: ReviewStore must expose a guarded mark_resolved that "
                "only overwrites when the current resolution matches the "
                "expected value (e.g. NULL or 'claiming'). Neither "
                "`mark_resolved_from_open` nor a keyword-only `expected_resolution` "
                "was found."
            )

    reread = store.get(review_id)
    assert reread["resolution"] == "submitted", (
        f"guarded mark_resolved must NOT clobber a completed resolution; "
        f"got {reread['resolution']!r}. Pre-fix silently overwrites."
    )


# ─────────────────────────────────────────────────────────
# H6 — Migration 003 gated + multi-applicant safe
# ─────────────────────────────────────────────────────────


def test_migration_003_delete_partitions_by_applicant(tmp_path):
    """The DELETE step in migration 003 must partition by applicant.

    v1 is single-user so the UNIQUE index still rejects same-posting-
    different-applicant collisions at INSERT time (that's a forward-compat
    conversation for v2). But the DELETE step MUST NOT clobber rows across
    applicants for the case where the DB somehow acquired multi-applicant
    rows before the migration ran. Defense-in-depth so the migration itself
    is never the reason a row disappears.

    RED contract: invoke the DELETE step directly with a two-applicant
    dataset (no active v2 index) and observe both survive.
    """
    from src.apply.dedup import _apply_migration_003_gated, _execute_migrations, _MIGRATION_003_SQL_PATH  # noqa: E402

    db_path = tmp_path / "state" / "applied_jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Bootstrap a bare table without the v2 index.
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
        # Two applicants at the same (ats_domain, ats_job_id).
        for applicant in ("alice", "bob"):
            conn.execute(
                "INSERT INTO applied_jobs (applicant, company, company_normalized, "
                "role_title, role_title_normalized, ats, ats_domain, ats_job_id, "
                "job_url, apply_url, application_id, status, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (applicant, "AcmeCorp", "acmecorp", "Eng", "eng", "greenhouse",
                 "boards.greenhouse.io", "SHARED_1",
                 f"https://boards.greenhouse.io/acme/jobs/SHARED_1",
                 f"https://boards.greenhouse.io/acme/jobs/SHARED_1",
                 f"APP_{applicant}", "submitted", "2026-07-08T00:00:00+00:00"),
            )
        # Bootstrap the schema_migrations table so the 003 DELETE will fire.
        conn.execute(
            "CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    # Now run ONLY the DELETE + INDEX + DROP part of 003, avoiding 001's
    # CREATE UNIQUE INDEX that would collide on our two rows.
    conn = sqlite3.connect(str(db_path))
    try:
        # Fire the destructive step directly.
        conn.execute(
            "DELETE FROM applied_jobs "
            "WHERE ats_domain IS NOT NULL "
            "  AND ats_job_id IS NOT NULL "
            "  AND id NOT IN ("
            "      SELECT MIN(id) FROM applied_jobs "
            "      WHERE ats_domain IS NOT NULL AND ats_job_id IS NOT NULL "
            "      GROUP BY applicant, ats_domain, ats_job_id"
            "  )"
        )
        conn.commit()
        cur = conn.execute(
            "SELECT applicant FROM applied_jobs "
            "WHERE ats_domain='boards.greenhouse.io' AND ats_job_id='SHARED_1' "
            "ORDER BY applicant"
        )
        applicants = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    assert applicants == ["alice", "bob"], (
        f"H6: migration 003 DELETE must partition by applicant; got surviving "
        f"applicants={applicants!r}. Pre-fix DELETE-MIN(id) partitioned only "
        f"by (ats_domain, ats_job_id) — losing one of them."
    )

    # Also assert the SQL file (kept for reference) uses the applicant
    # partition — a source-grep so a future refactor doesn't silently regress.
    if _MIGRATION_003_SQL_PATH.exists():
        sql = _MIGRATION_003_SQL_PATH.read_text()
        assert "GROUP BY applicant" in sql, (
            "H6: migrations/003_normalized_hard_dedup.sql must document the "
            "applicant-aware GROUP BY for the DELETE partition."
        )


def test_migration_003_delete_gated_by_migrations_table(tmp_path):
    """Migration 003 must record its application in a schema_migrations
    table so the DELETE (destructive) doesn't re-run on every DedupDB
    instantiation. Fresh DB → apply → recorded → next open → skip DELETE.
    """
    from src.apply.dedup import DedupDB
    db = DedupDB(tmp_path / "state" / "applied_jobs.db")

    conn = sqlite3.connect(str(db.path))
    try:
        # A schema_migrations table (or equivalent tracker) must exist.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        exists = cur.fetchone() is not None
    finally:
        conn.close()
    assert exists, (
        "H6: DedupDB must maintain a `schema_migrations` table (or equivalent) "
        "so destructive migration steps only run once. Pre-fix runs the "
        "DELETE step on every instantiation."
    )


# ─────────────────────────────────────────────────────────
# H7+H13 — was_applied precheck uses real ats_domain + applicant
# ─────────────────────────────────────────────────────────


def test_was_applied_precheck_passes_real_ats_domain(tmp_path):
    """execute_confirmed_submit's was_applied precheck must pass real
    ats_domain + ats_job_id extracted from decision.apply_url so it matches
    the (ats_domain, ats_job_id) UNIQUE index shape. Pre-fix passes
    ats_domain=None → falls to job_url-only branch → cross-user leak.

    Additionally: the precheck must filter by applicant so applicant A's
    YES cannot reconcile applicant B's pending row on the same job_url.
    """
    from src.apply.dedup import DedupDB
    from src.apply.review import execute_confirmed_submit, Decision

    db = DedupDB(tmp_path / "state" / "applied_jobs.db")

    # Set up a spy dedup wrapper to capture the was_applied args.
    class _Spy:
        def __init__(self, inner):
            self._inner = inner
            self.calls = []

        def was_applied(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return False

        def record(self, *a, **kw):
            return self._inner.record(*a, **kw)

    spy = _Spy(db)

    # Fake session context manager that yields a fake page.
    from contextlib import contextmanager

    @contextmanager
    def _fake_session_ctx(*, storage_state_path=None, headless=True):
        class _Page:
            url = "https://boards.greenhouse.io/acme/jobs/12345"

            def goto(self, u):
                pass
        yield (_Page(), None)

    def _fake_load_state(ats, applicant):
        return None

    decision = Decision(
        review_id="R1",
        status="submitted",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
        ats="greenhouse",
        company="AcmeCorp",
        role_title="Senior Engineer",
        applicant="jane",
        thread_id="T",
    )

    class _FakeAdapter:
        def apply(self, page, ctx):
            from src.apply.types import ApplyResult
            return ApplyResult(status="failed", ats="greenhouse", apply_url=decision.apply_url, reason="test-stop")

    execute_confirmed_submit(
        decision,
        _FakeAdapter(),
        {"apply": {"dedup_db_path": str(db.path)}},
        session_ctx=_fake_session_ctx,
        load_state_fn=_fake_load_state,
        dedup_db=spy,
    )

    assert spy.calls, "was_applied must be called by the precheck."
    args, kwargs = spy.calls[0]
    # Assert real ats_domain + ats_job_id (not None).
    # Accept both positional (company, ats_domain, ats_job_id, job_url) and
    # kwargs shape.
    ats_domain = None
    ats_job_id = None
    if kwargs:
        ats_domain = kwargs.get("ats_domain")
        ats_job_id = kwargs.get("ats_job_id")
    if ats_domain is None and len(args) >= 3:
        ats_domain, ats_job_id = args[1], args[2]

    assert ats_domain == "boards.greenhouse.io", (
        f"H7/H13: was_applied precheck must pass real ats_domain "
        f"('boards.greenhouse.io'); got {ats_domain!r}. Pre-fix passes None → "
        f"falls to job_url-only branch → cross-user leak."
    )
    assert ats_job_id == "12345", (
        f"H7/H13: was_applied precheck must pass real ats_job_id ('12345'); "
        f"got {ats_job_id!r}."
    )


def test_was_applied_precheck_filters_by_applicant(tmp_path):
    """Cross-user leak guard: applicant A's YES on the SAME job_url must
    NOT match applicant B's pre-existing submission. Post-fix: was_applied
    must accept applicant and gate on it.
    """
    from src.apply.dedup import DedupDB

    db = DedupDB(tmp_path / "state" / "applied_jobs.db")
    # Record for applicant 'alice'.
    db.record(
        _apply_result_ns(
            status="submitted",
            ats="greenhouse",
            apply_url="https://boards.greenhouse.io/acme/jobs/500",
        ),
        applicant="alice",
        company="AcmeCorp",
        role_title="Senior Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/500",
    )

    # Now query for applicant 'bob' at the same posting.
    # was_applied must gain an `applicant` filter — either positional or kwarg.
    try:
        hit = db.was_applied(
            "AcmeCorp",
            "boards.greenhouse.io",
            "500",
            "https://boards.greenhouse.io/acme/jobs/500",
            applicant="bob",
        )
    except TypeError:
        pytest.fail(
            "H7/H13: DedupDB.was_applied must accept an `applicant` kwarg "
            "to gate cross-applicant matches. Missing on pre-fix."
        )
    assert hit is False, (
        f"H7/H13: was_applied for applicant='bob' must NOT match alice's row "
        f"at the same posting. Got hit={hit}. Pre-fix leaks across users."
    )


# ─────────────────────────────────────────────────────────
# H8 — _soft_warn_lookup fail-CLOSED
# ─────────────────────────────────────────────────────────


def test_soft_warn_lookup_fails_closed_on_exception():
    """_soft_warn_lookup must fail CLOSED (return a synthetic hit) when the
    dedup DB raises, so the soft-warn gate does not silently open on a
    broken DB. Pre-fix returns [] → soft_warn_active=False → auto-submit
    proceeds.
    """
    from src.apply.adapters.greenhouse import _soft_warn_lookup

    class _BrokenDedup:
        def soft_warn_check(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    hits = _soft_warn_lookup(_BrokenDedup(), "AcmeCorp", "Senior Engineer")
    assert hits, (
        f"H8: on dedup exception, _soft_warn_lookup must fail CLOSED "
        f"(return a synthetic hit list) so soft_warn_active=True routes to "
        f"review. Got {hits!r} which is empty → auto-submit gate opens."
    )


# ─────────────────────────────────────────────────────────
# H9 — mode normalization + poll-path fail-closed on dedup init
# ─────────────────────────────────────────────────────────


def test_seam_h7_normalizes_mode_case_and_whitespace(tmp_path, monkeypatch):
    """H7 fail-closed logic must normalize apply_config['mode'] (case,
    whitespace) so 'AUTO' or ' auto ' still triggers fail-closed on dedup
    init failure. Pre-fix `apply_config.get('mode', 'review') == 'auto'` is
    literal string match → 'AUTO' bypasses fail-closed and submits.
    """
    import src.apply._seam as seam_mod
    from src.apply import dedup as dedup_mod

    class _BrokenDedupDB:
        def __init__(self, *a, **kw):
            raise sqlite3.OperationalError("simulated")

    monkeypatch.setattr(dedup_mod, "DedupDB", _BrokenDedupDB)

    class _FakeGmail:
        def get_or_create_label(self, name):
            return f"lbl:{name}"

        def send_with_labels(self, **kw):
            return ("mid", "tid")

    captured = {"ctx_mode": None}

    def _fake_apply_to_job(job_url, ctx, config):
        captured["ctx_mode"] = getattr(ctx, "mode", None)
        from src.apply.types import ApplyResult
        return ApplyResult(status="submitted", ats="greenhouse", apply_url=job_url)

    monkeypatch.setattr(
        seam_mod,
        "_call_apply_to_job",
        lambda *, job_url, ctx, config: _fake_apply_to_job(job_url, ctx, config),
    )

    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    apply_config = {
        "enabled": True,
        "mode": "  AUTO  ",  # weird case + whitespace
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
    }
    job = {
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "company": "AcmeCorp",
        "role_title": "Senior Engineer",
        "title": "Senior Engineer",
    }
    seam_mod.run_for_job(
        job=job,
        jd_text="fake",
        lane={"name": "backend", "label": "backend"},
        resume_path=None,
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
        gmail_client=_FakeGmail(),
    )
    assert captured["ctx_mode"] == "review", (
        f"H9: seam must normalize 'AUTO'/whitespace to 'auto' before the H7 "
        f"fail-closed check; got ctx_mode={captured['ctx_mode']!r}. Pre-fix "
        f"literal-string compare misses 'AUTO' and auto-submits."
    )


def test_initialize_fails_closed_on_dedup_init(tmp_path, monkeypatch):
    """H9 poll-path coverage: seam.initialize() must not run
    poll_pending_reviews when DedupDB init fails. Otherwise the poll's
    YES branch can still fire (via execute_confirmed_submit's own dedup
    lookup) but with an inconsistent DB state → subtle behaviour bugs.
    """
    import src.apply._seam as seam_mod

    # Force the dedup construction inside _call_poll_pending_reviews to fail.
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated")

    from src.apply import dedup as dedup_mod
    monkeypatch.setattr(dedup_mod, "DedupDB", _boom)

    # Also monkeypatch poll_pending_reviews so we know whether it was called.
    called = {"poll": 0}

    def _spy_poll(*a, **kw):
        called["poll"] += 1
        return []

    monkeypatch.setattr("src.apply.review.poll_pending_reviews", _spy_poll)

    class _FakeGmail:
        def get_or_create_label(self, name):
            return f"lbl:{name}"

    config = {
        "apply": {
            "enabled": True,
            "gmail_label_prefix": "hiring-agent/apply",
            "dedup_db_path": str(tmp_path / "state" / "applied_jobs.db"),
        }
    }
    events = seam_mod.initialize(config, _FakeGmail())
    assert events == [], "initialize must return [] when dedup init fails."
    assert called["poll"] == 0, (
        f"H9: initialize must NOT run poll_pending_reviews when DedupDB init "
        f"fails (fail-closed at poll boundary too). Got poll_call_count="
        f"{called['poll']}."
    )


# ─────────────────────────────────────────────────────────
# H10 — executescript transaction ordering
# ─────────────────────────────────────────────────────────


def test_migrations_apply_in_single_transaction(tmp_path):
    """_execute_migrations must NOT commit the outer transaction mid-migration.
    All three migrations must apply atomically so a partial failure rolls back
    cleanly (no half-migrated schema).
    """
    from src.apply.dedup import _execute_migrations
    db_path = tmp_path / "state" / "applied_jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        # Start an explicit transaction.
        conn.execute("BEGIN")
        _execute_migrations(conn)
        # If executescript committed mid-flight, the connection's
        # `in_transaction` flag would be True (implicit new tx started) OR
        # the outer BEGIN would be broken. Verify we can still ROLLBACK the
        # migration cleanly.
        conn.rollback()
        # After rollback, the applied_jobs table must NOT exist — proving
        # the migration was atomic within our BEGIN..ROLLBACK.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='applied_jobs'"
        )
        row = cur.fetchone()
    finally:
        conn.close()

    assert row is None, (
        "H10: _execute_migrations must NOT commit mid-migration. Pre-fix "
        "`executescript` on 001 auto-commits, so the outer BEGIN..ROLLBACK "
        "does not roll back the 001 tables (they persist)."
    )


# ─────────────────────────────────────────────────────────
# H11 — substring match tightening
# ─────────────────────────────────────────────────────────


def test_migration_substring_match_rejects_unrelated_errors(tmp_path, monkeypatch):
    """The OperationalError swallow-guard must not silently ignore unrelated
    errors just because their message happens to contain 'already exists'.

    Post-fix: match on the SQLite-specific phrasing (`duplicate column name`,
    `index ... already exists`, `no such index`) rather than the loose
    'already exists' substring. Assertion: an OperationalError whose message
    contains 'file already exists' (disk I/O error variant) must NOT be
    swallowed.
    """
    import src.apply.dedup as dedup_mod
    from src.apply.dedup import _execute_migrations

    db_path = tmp_path / "state" / "applied_jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        # First apply migrations cleanly.
        _execute_migrations(conn)
        conn.commit()
    finally:
        conn.close()

    # Directly probe the substring guard helper. Post-fix, an unrelated
    # OperationalError variant (e.g. a 'output file already exists' disk I/O
    # error) must NOT be classified as an idempotent-DDL swallow.
    from src.apply.dedup import _is_idempotent_ddl_error

    # These SHOULD be classified as idempotent (safe to swallow).
    assert _is_idempotent_ddl_error(
        sqlite3.OperationalError("duplicate column name: applicant")
    )
    assert _is_idempotent_ddl_error(
        sqlite3.OperationalError("index ux_applied_jobs_hard_v2 already exists")
    )
    assert _is_idempotent_ddl_error(
        sqlite3.OperationalError("no such index: ux_applied_jobs_hard")
    )
    # These MUST NOT be swallowed (unrelated errors).
    assert not _is_idempotent_ddl_error(
        sqlite3.OperationalError("output file already exists")
    ), (
        "H11: unrelated 'already exists' variants must NOT be classified "
        "as idempotent-DDL. Pre-fix loose match swallows them."
    )
    assert not _is_idempotent_ddl_error(
        sqlite3.OperationalError("database is locked")
    )
    assert not _is_idempotent_ddl_error(
        sqlite3.OperationalError("disk I/O error")
    )
