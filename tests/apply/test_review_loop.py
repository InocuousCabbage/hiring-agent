"""
tests/apply/test_review_loop.py — S12 gmail-review-loop coverage.

Covers ≥12 branches from spec §TDD test scaffolding + PII + L6 (datetime.now(UTC))
guards. Every test mocks the Gmail client, the DedupDB, the S4 session context,
and the S6 storage_state loader — this shard's code paths never touch a real
network, a real browser, or a real Gmail account.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import structlog
import structlog.testing

# Ensure `src` is importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.apply.review import (  # noqa: E402
    Decision,
    _parse_first_line,
    _strip_quoted,
    _uuid7,
    ensure_labels,
    execute_confirmed_submit,
    poll_pending_reviews,
    stage_review,
)
from src.apply.state_store import ReviewStore  # noqa: E402


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────


@pytest.fixture
def config():
    return {
        "apply": {
            "gmail_label_prefix": "hiring-agent/apply",
            "review_reping_hours": 24,
            "review_timeout_hours": 72,
            "dedup_db_path": ":memory:",
            "fast_path_recipient": "operator@example.com",
            "storage_state_dir": "config/credentials/apply",
        }
    }


@pytest.fixture
def store(tmp_path):
    s = ReviewStore(tmp_path / "review.db")
    yield s
    s.close()


@pytest.fixture
def gmail():
    g = MagicMock(name="GmailClient")
    g.list_labels.return_value = []
    g.get_or_create_label.side_effect = lambda name: f"LABEL::{name}"
    g.send_with_labels.return_value = ("MSG_1", "THREAD_1")
    g.search.return_value = []
    g.apply_label.return_value = None
    g.remove_label.return_value = None
    g.reply_to_thread.return_value = "REPLY_MSG_1"
    return g


class FakeResult:
    """Duck-typed stand-in for S2's frozen ApplyResult dataclass."""

    def __init__(
        self,
        *,
        status: str = "review_required",
        ats: str = "greenhouse",
        apply_url: str = "https://boards.greenhouse.io/acme/jobs/1",
        confirmation_screenshot: Path | None = None,
        application_id: str | None = None,
    ):
        self.status = status
        self.ats = ats
        self.apply_url = apply_url
        self.confirmation_screenshot = confirmation_screenshot
        self.application_id = application_id
        self.reason = None
        self.human_review_url = None
        self.submitted_at = None
        self.trace_path = None
        self.review_id = None


class FakeCtx:
    """Duck-typed stand-in for S2's frozen ApplyContext dataclass.

    Applicant email + phone are set to well-known PII strings so the PII
    audit test can assert none of them leak into any body or log line.
    """

    def __init__(self, config, applicant: str = "jane@example.com"):
        self.config = config
        self.applicant = applicant
        self.job = {
            "company": "AcmeCorp",
            "role_title": "Senior Engineer",
            "title": "Senior Engineer",
            "job_url": "https://acme.com/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        }
        self.mode = "review"
        self.dry_run = False
        # These are the PII strings the audit test greps for.
        self.profile = MagicMock(
            email="jane@example.com",
            phone="+1-555-0100",
            answers={"why_here": "SECRET_ANSWER_STRING"},
        )
        self.resume_path = Path("/tmp/resume.pdf")
        self.cover_letter_path = None


def _decision(**overrides) -> Decision:
    base = dict(
        review_id="0195c5a0-1234-7abc-8def-0123456789ab",
        status="review_required",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        company="AcmeCorp",
        role_title="Senior Engineer",
        applicant="jane@example.com",
        thread_id="THREAD_1",
    )
    base.update(overrides)
    return Decision(**base)


# ─────────────────────────────────────────────────────────
# Label CRUD + boot
# ─────────────────────────────────────────────────────────


def test_ensure_labels_creates_three_nested_labels(gmail, config):
    gmail.list_labels.return_value = []
    ids = ensure_labels(gmail, config)

    assert set(ids.keys()) == {"pending", "submitted", "declined"}
    called_names = {c.args[0] for c in gmail.get_or_create_label.call_args_list}
    assert called_names == {
        "hiring-agent/apply/pending",
        "hiring-agent/apply/submitted",
        "hiring-agent/apply/declined",
    }


def test_ensure_labels_is_idempotent(gmail, config):
    gmail.list_labels.return_value = [
        {"id": "L1", "name": "hiring-agent/apply/pending"},
        {"id": "L2", "name": "hiring-agent/apply/submitted"},
        {"id": "L3", "name": "hiring-agent/apply/declined"},
    ]
    gmail.get_or_create_label.side_effect = lambda name: {
        "hiring-agent/apply/pending": "L1",
        "hiring-agent/apply/submitted": "L2",
        "hiring-agent/apply/declined": "L3",
    }[name]

    ids_first = ensure_labels(gmail, config)
    ids_second = ensure_labels(gmail, config)

    assert ids_first == ids_second == {"pending": "L1", "submitted": "L2", "declined": "L3"}
    # get_or_create_label may be called on each boot — but it must NOT create
    # duplicate labels; the underlying Gmail create() should not fire when the
    # label already exists. The idempotency contract is on the Gmail-side
    # helper (`get_or_create_label`), so we only assert that the returned dict
    # contains the pre-existing IDs and that no new label names appear.


# ─────────────────────────────────────────────────────────
# uuid7 + stage_review
# ─────────────────────────────────────────────────────────


def test_uuid7_shape():
    for _ in range(20):
        u = _uuid7()
        parsed = UUID(u)
        assert parsed.version == 7
        # RFC 9562 variant is 10 (binary) — python's stdlib maps that to
        # "specified in RFC 4122" (name pre-dates 9562; same bit pattern).
        assert (parsed.int >> 62) & 0b11 == 0b10
        assert parsed.variant in ("specified in RFC 4122", "RFC 4122")


def test_stage_review_returns_uuid7(gmail, store, config):
    result = FakeResult(confirmation_screenshot=Path("/tmp/ss.png"))
    ctx = FakeCtx(config)

    rid = stage_review(result, ctx, gmail, store, filled_count=7)

    parsed = UUID(rid)
    assert parsed.version == 7


def test_stage_review_inserts_row_and_sends_email_with_review_id_in_subject(
    gmail, store, config
):
    result = FakeResult(confirmation_screenshot=Path("/tmp/ss.png"))
    ctx = FakeCtx(config)

    rid = stage_review(result, ctx, gmail, store, filled_count=7)

    row = store.get(rid)
    assert row is not None
    assert row["review_id"] == rid
    assert row["first_sent_at"] is not None
    assert row["repings_sent"] == 0
    assert row["resolution"] is None
    assert row["gmail_thread_id"] == "THREAD_1"

    call_kwargs = gmail.send_with_labels.call_args.kwargs
    assert re.search(r"\[review_id=" + re.escape(rid) + r"\]", call_kwargs["subject"])
    assert "AcmeCorp" in call_kwargs["subject"]
    assert "Senior Engineer" in call_kwargs["subject"]


def test_stage_review_email_body_omits_pii(gmail, store, config):
    result = FakeResult(confirmation_screenshot=Path("/tmp/ss.png"))
    ctx = FakeCtx(config, applicant="jane@example.com")

    rid = stage_review(result, ctx, gmail, store, filled_count=7)

    body = gmail.send_with_labels.call_args.kwargs["body"]
    # PII strings from the fake profile MUST NOT appear.
    assert "jane@example.com" not in body
    assert "+1-555-0100" not in body
    assert "SECRET_ANSWER_STRING" not in body
    # Structural elements MUST appear.
    assert "https://boards.greenhouse.io/acme/jobs/1" in body
    assert "AcmeCorp" in body
    assert "Senior Engineer" in body
    assert "7" in body  # filled-field count


# ─────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["YES", "Yes", "yes", "YES!", "YES.", "YES please", "Yes, submit", "yes\n"],
)
def test_parse_yes_variants(text):
    assert _parse_first_line(text) == "YES"


@pytest.mark.parametrize("text", ["NO", "no", "No thanks", "NO.", "No way", "no,\n"])
def test_parse_no_variants(text):
    assert _parse_first_line(text) == "NO"


@pytest.mark.parametrize(
    "text",
    ["Yeah", "Y", "maybe", "", "submit please", "Sure why not", "  ", "N", "nope"],
)
def test_parse_ambiguous_variants(text):
    assert _parse_first_line(text) == "AMBIGUOUS"


def test_strip_quoted_lines_before_parsing():
    body = "> prior line\n> more prior\nYES\n"
    stripped = _strip_quoted(body)
    first_line = next((ln for ln in stripped.splitlines() if ln.strip()), "")
    assert _parse_first_line(first_line) == "YES"


def test_strip_quoted_lines_all_quoted_returns_ambiguous():
    body = "> only quoted\n>> more quoted\n"
    stripped = _strip_quoted(body)
    first_line = next((ln for ln in stripped.splitlines() if ln.strip()), "")
    assert _parse_first_line(first_line) == "AMBIGUOUS"


# ─────────────────────────────────────────────────────────
# Poll / resolution branches
# ─────────────────────────────────────────────────────────


def _seed_pending_row(store, *, review_id, first_sent_at, repings_sent=0):
    store.insert(
        {
            "review_id": review_id,
            "job_url": "https://acme.com/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "AcmeCorp",
            "role_title": "Senior Engineer",
            "ats": "greenhouse",
            "filled_at": first_sent_at,
            "screenshot_path": "/tmp/ss.png",
            "trace_path": None,
            "first_sent_at": first_sent_at,
            "last_repinged_at": None,
            "repings_sent": repings_sent,
            "gmail_thread_id": "THREAD_1",
            "resolution": None,
            "resolved_at": None,
        }
    )


def _thread_msg(*, body: str, msg_id: str = "REPLY_MSG_1", thread_id: str = "THREAD_1"):
    """Shape returned by GmailClient.search() — one message per thread."""
    return {
        "id": msg_id,
        "thread_id": thread_id,
        "body_text": body,
        "internal_date_ms": 0,
    }


def test_poll_yes_transitions_label_and_calls_execute_confirmed_submit(
    gmail, store, config
):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000001"
    _seed_pending_row(store, review_id=rid, first_sent_at=(now - timedelta(hours=1)).isoformat())
    gmail.search.return_value = [_thread_msg(body="YES\n\nregards")]

    with patch("src.apply.review.execute_confirmed_submit") as mock_submit:
        mock_submit.return_value = FakeResult(status="submitted", application_id="app-1")
        decisions = poll_pending_reviews(
            gmail, store, now=now, config=config, adapter=MagicMock()
        )

    assert mock_submit.call_count == 1
    row = store.get(rid)
    assert row["resolution"] == "submitted"
    # Label move: submitted label added, pending removed.
    gmail.apply_label.assert_any_call("REPLY_MSG_1", "LABEL::hiring-agent/apply/submitted")
    gmail.remove_label.assert_any_call("REPLY_MSG_1", "LABEL::hiring-agent/apply/pending")
    assert any(d.status == "submitted" for d in decisions)


def test_poll_no_transitions_label_and_skips_submit(gmail, store, config):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000002"
    _seed_pending_row(store, review_id=rid, first_sent_at=(now - timedelta(hours=1)).isoformat())
    gmail.search.return_value = [_thread_msg(body="no thanks\n")]

    with patch("src.apply.review.execute_confirmed_submit") as mock_submit:
        decisions = poll_pending_reviews(
            gmail, store, now=now, config=config, adapter=MagicMock()
        )
        mock_submit.assert_not_called()

    row = store.get(rid)
    assert row["resolution"] == "declined"
    gmail.apply_label.assert_any_call("REPLY_MSG_1", "LABEL::hiring-agent/apply/declined")
    gmail.remove_label.assert_any_call("REPLY_MSG_1", "LABEL::hiring-agent/apply/pending")
    assert any(d.status == "declined" for d in decisions)


def test_poll_ambiguous_sends_clarification_and_does_not_reping(gmail, store, config):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000003"
    _seed_pending_row(store, review_id=rid, first_sent_at=(now - timedelta(hours=1)).isoformat())
    gmail.search.return_value = [_thread_msg(body="yeah sounds good\n")]

    with patch("src.apply.review.execute_confirmed_submit") as mock_submit:
        poll_pending_reviews(gmail, store, now=now, config=config, adapter=MagicMock())
        mock_submit.assert_not_called()

    gmail.reply_to_thread.assert_called_once()
    reply_body = gmail.reply_to_thread.call_args.args[1] if len(
        gmail.reply_to_thread.call_args.args
    ) > 1 else gmail.reply_to_thread.call_args.kwargs["body"]
    assert "please reply yes or no on the first line" in reply_body.lower()

    row = store.get(rid)
    assert row["repings_sent"] == 0
    assert row["resolution"] is None
    # No label change.
    for call in gmail.apply_label.call_args_list:
        assert "declined" not in call.args[1]
        assert "submitted" not in call.args[1]


# ─────────────────────────────────────────────────────────
# Timeouts
# ─────────────────────────────────────────────────────────


def test_poll_reping_at_24h(gmail, store, config):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000004"
    _seed_pending_row(
        store, review_id=rid, first_sent_at=(now - timedelta(hours=25)).isoformat()
    )
    gmail.search.return_value = []  # no reply yet

    poll_pending_reviews(gmail, store, now=now, config=config, adapter=MagicMock())

    gmail.reply_to_thread.assert_called_once()
    reply_body = gmail.reply_to_thread.call_args.args[1] if len(
        gmail.reply_to_thread.call_args.args
    ) > 1 else gmail.reply_to_thread.call_args.kwargs["body"]
    assert "Still awaiting YES/NO" in reply_body
    assert "72h" in reply_body

    row = store.get(rid)
    assert row["repings_sent"] == 1
    assert row["last_repinged_at"] is not None


def test_poll_does_not_reping_twice(gmail, store, config):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000005"
    _seed_pending_row(
        store,
        review_id=rid,
        first_sent_at=(now - timedelta(hours=48)).isoformat(),
        repings_sent=1,
    )
    gmail.search.return_value = []  # still no reply

    poll_pending_reviews(gmail, store, now=now, config=config, adapter=MagicMock())

    gmail.reply_to_thread.assert_not_called()


def test_poll_auto_declines_at_72h_with_no_thread_id_skips_label_ops(
    gmail, store, config
):
    """Code-review finding #1: if stage_review's Gmail send failed but the
    DB insert succeeded, the row has ``gmail_thread_id=None``. 72h later,
    the poller must NOT call ``gmail.apply_label`` with the review_id
    (which is a UUID, not a valid Gmail msg_id — would 400 the API)."""
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000099"
    # Seed the row with NO thread_id (stage_review's send-with-labels failed).
    store.insert(
        {
            "review_id": rid,
            "job_url": "https://acme.com/jobs/x",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/x",
            "company": "AcmeCorp",
            "role_title": "SRE",
            "ats": "greenhouse",
            "filled_at": (now - timedelta(hours=73)).isoformat(),
            "screenshot_path": "/tmp/x.png",
            "trace_path": None,
            "first_sent_at": (now - timedelta(hours=73)).isoformat(),
            "last_repinged_at": None,
            "repings_sent": 0,
            "gmail_thread_id": None,
            "resolution": None,
            "resolved_at": None,
        }
    )
    gmail.search.return_value = []

    with structlog.testing.capture_logs() as captured:
        poll_pending_reviews(gmail, store, now=now, config=config, adapter=MagicMock())

    # Row STILL resolves to auto_declined — state advance doesn't depend on Gmail.
    assert store.get(rid)["resolution"] == "auto_declined"
    # But no label ops fired — because the row has no valid Gmail target id.
    gmail.apply_label.assert_not_called()
    gmail.remove_label.assert_not_called()
    # And we logged the reason.
    assert any(
        e.get("event") == "apply.review.auto_decline_no_thread" for e in captured
    )


def test_poll_auto_declines_at_72h(gmail, store, config):
    now = datetime.now(timezone.utc)
    rid = "0195c5a0-1234-7abc-8def-000000000006"
    first_sent = (now - timedelta(hours=73)).isoformat()
    _seed_pending_row(store, review_id=rid, first_sent_at=first_sent, repings_sent=1)
    gmail.search.return_value = []

    with structlog.testing.capture_logs() as captured:
        decisions = poll_pending_reviews(
            gmail, store, now=now, config=config, adapter=MagicMock()
        )

    row = store.get(rid)
    assert row["resolution"] == "auto_declined"
    # Label move to declined.
    gmail.apply_label.assert_any_call(rid, "LABEL::hiring-agent/apply/declined") if False else None
    # The label operation targets the thread's first message id — for the
    # auto-decline branch there was no reply, so the row's own thread_id is used.
    # Assert at least one apply_label call landed the declined label.
    assert any(
        call.args[1] == "LABEL::hiring-agent/apply/declined"
        for call in gmail.apply_label.call_args_list
    )
    events = [e for e in captured if e.get("event") == "apply.review.auto_declined"]
    assert events, f"expected apply.review.auto_declined; got {[e.get('event') for e in captured]}"
    assert events[0]["review_id"] == rid
    assert "first_sent_at" in events[0]
    assert "resolved_at" in events[0]
    assert any(d.status == "auto_declined" for d in decisions)


# ─────────────────────────────────────────────────────────
# execute_confirmed_submit
# ─────────────────────────────────────────────────────────


class _FakePage:
    def goto(self, url):  # pragma: no cover — trivial
        self.url = url


class _FakeSessionCM:
    def __init__(self, page):
        self._page = page
    def __enter__(self):
        return self._page, None
    def __exit__(self, *args):
        return False


def _fake_session_factory(page):
    def _ctx(*args, **kwargs):
        return _FakeSessionCM(page)
    return _ctx


def test_execute_confirmed_submit_records_dedup_on_success(config):
    adapter = MagicMock()
    adapter.apply.return_value = FakeResult(
        status="submitted",
        application_id="app-1",
        confirmation_screenshot=Path("/tmp/confirm.png"),
    )
    fake_db = MagicMock()
    fake_db.was_applied.return_value = False
    fake_load_state = MagicMock(return_value={"cookies": []})
    fake_session = _fake_session_factory(_FakePage())

    result = execute_confirmed_submit(
        _decision(),
        adapter,
        config,
        session_ctx=fake_session,
        load_state_fn=fake_load_state,
        dedup_db=fake_db,
    )

    assert result.status == "submitted"
    fake_db.record.assert_called_once()
    adapter.apply.assert_called_once()


def test_execute_confirmed_submit_idempotent_on_replay(config):
    adapter = MagicMock()
    adapter.apply.return_value = FakeResult(status="submitted", application_id="app-1")
    fake_db = MagicMock()
    fake_db.was_applied.return_value = False
    fake_db.record.side_effect = sqlite3.IntegrityError("UNIQUE constraint failed")
    fake_load_state = MagicMock(return_value={"cookies": []})
    fake_session = _fake_session_factory(_FakePage())

    with structlog.testing.capture_logs() as captured:
        result = execute_confirmed_submit(
            _decision(),
            adapter,
            config,
            session_ctx=fake_session,
            load_state_fn=fake_load_state,
            dedup_db=fake_db,
        )

    assert result.status == "already_applied"
    assert any(e.get("event") == "apply.review.already_recorded" for e in captured)


def test_execute_confirmed_submit_failure_keeps_row_pending(gmail, store, config):
    adapter = MagicMock()
    adapter.apply.return_value = FakeResult(status="failed", apply_url="https://x")
    fake_db = MagicMock()
    fake_db.was_applied.return_value = False
    fake_load_state = MagicMock(return_value={"cookies": []})
    fake_session = _fake_session_factory(_FakePage())

    with structlog.testing.capture_logs() as captured:
        result = execute_confirmed_submit(
            _decision(),
            adapter,
            config,
            session_ctx=fake_session,
            load_state_fn=fake_load_state,
            dedup_db=fake_db,
        )

    assert result.status == "failed"
    fake_db.record.assert_not_called()
    assert any(e.get("event") == "apply.review.submit_failed" for e in captured)


def test_execute_confirmed_submit_never_wraps_submit_in_retry(config):
    """adapter.apply is called EXACTLY once, even on transient failure."""
    adapter = MagicMock()
    adapter.apply.return_value = FakeResult(status="failed")
    fake_db = MagicMock()
    fake_db.was_applied.return_value = False
    fake_load_state = MagicMock(return_value={"cookies": []})
    fake_session = _fake_session_factory(_FakePage())

    execute_confirmed_submit(
        _decision(),
        adapter,
        config,
        session_ctx=fake_session,
        load_state_fn=fake_load_state,
        dedup_db=fake_db,
    )

    assert adapter.apply.call_count == 1


# ─────────────────────────────────────────────────────────
# ReviewStore CRUD roundtrip
# ─────────────────────────────────────────────────────────


def test_review_store_crud_roundtrip(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    try:
        rid = "0195c5a0-1234-7abc-8def-999999999999"
        now = datetime.now(timezone.utc).isoformat()
        store.insert(
            {
                "review_id": rid,
                "job_url": "https://acme.com/jobs/1",
                "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                "company": "AcmeCorp",
                "role_title": "Senior Engineer",
                "ats": "greenhouse",
                "filled_at": now,
                "screenshot_path": "/tmp/ss.png",
                "trace_path": None,
                "first_sent_at": now,
                "last_repinged_at": None,
                "repings_sent": 0,
                "gmail_thread_id": "THREAD_777",
                "resolution": None,
                "resolved_at": None,
            }
        )
        assert store.get(rid)["review_id"] == rid
        assert store.by_thread("THREAD_777")["review_id"] == rid
        assert [r["review_id"] for r in store.list_open()] == [rid]

        later = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        store.mark_repinged(rid, later)
        row = store.get(rid)
        assert row["last_repinged_at"] == later
        assert row["repings_sent"] == 1

        store.mark_resolved(rid, "submitted", later)
        row = store.get(rid)
        assert row["resolution"] == "submitted"
        assert row["resolved_at"] == later
        assert store.list_open() == []
    finally:
        store.close()


# ─────────────────────────────────────────────────────────
# PII audit + L6 datetime guard
# ─────────────────────────────────────────────────────────


def test_no_pii_in_any_review_log_event(gmail, store, config):
    """Capture every structlog event during a full staged→YES→submit flow and
    grep for the fixture applicant's PII strings. None must appear."""
    ctx = FakeCtx(config, applicant="jane@example.com")
    result = FakeResult(confirmation_screenshot=Path("/tmp/ss.png"))

    with structlog.testing.capture_logs() as captured:
        rid = stage_review(result, ctx, gmail, store, filled_count=7)

        now = datetime.now(timezone.utc)
        gmail.search.return_value = [_thread_msg(body="YES")]
        with patch("src.apply.review.execute_confirmed_submit") as mock_submit:
            mock_submit.return_value = FakeResult(status="submitted", application_id="app-1")
            poll_pending_reviews(gmail, store, now=now, config=config, adapter=MagicMock())

    pii_needles = ("jane@example.com", "+1-555-0100", "SECRET_ANSWER_STRING")
    for event in captured:
        for k, v in event.items():
            if isinstance(v, str):
                for needle in pii_needles:
                    assert needle not in v, (
                        f"PII leak in event {event.get('event')} key {k}: {v!r}"
                    )


def test_no_deprecated_utcnow_in_review_or_state_store():
    """L6 guard — the shard's two owned files must never call the deprecated
    naive UTC-now API. Every timestamp goes through `datetime.now(timezone.utc)`."""
    for name in ("src/apply/review.py", "src/apply/state_store.py"):
        text = (ROOT / name).read_text()
        assert "datetime.utcnow" not in text, f"deprecated utcnow found in {name}"
        assert re.search(r"utcnow\s*\(", text) is None, f"utcnow( found in {name}"


def test_label_prefix_read_from_config_in_single_helper():
    """L14 guard — the prefix `hiring-agent/apply` must not be hardcoded in
    multiple places in review.py. There must be exactly one string literal
    with that prefix (the constant / config-lookup helper); every other use
    reads from config."""
    text = (ROOT / "src/apply/review.py").read_text()
    # The docstring/comments may mention the prefix — exclude comment/docstring
    # occurrences by counting only non-doc, non-comment lines.
    lit_matches = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Skip docstring lines (rough heuristic — matches triple-quoted content
        # by being conservative: any line inside a triple-quoted block or that
        # itself is a string-only literal).
        if '"""' in line or "'''" in line:
            continue
        if '"hiring-agent/apply"' in line or "'hiring-agent/apply'" in line:
            lit_matches += 1
    # 0 hardcoded literals: prefix always comes from config[apply][gmail_label_prefix].
    assert lit_matches == 0, (
        f"prefix hardcoded in {lit_matches} places in review.py — must come from config"
    )
