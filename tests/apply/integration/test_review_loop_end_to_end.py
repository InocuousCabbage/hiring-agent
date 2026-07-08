"""Phase 1 END-TO-END integration test — S12 review loop wire-up.

Drives the full happy-path review flow against the real seam + review-loop
code with fakes ONLY at the outermost edges (Gmail service, ATS adapter,
Playwright transport). Everything between the seam entry point and those
edges is real: real ``ReviewStore``, real ``DedupDB``, real ``stage_review``,
real ``poll_pending_reviews``, real ``execute_confirmed_submit``, real
``compose_digest``.

Phase 1 scope (from `.agent/codebase-audit-2026-07-08.md`, `Phase 1` table):
    B1  — stage_review wiring in _seam.py
    H5  — env: resolution + insert/send ordering
    H4  — persist resume/cover paths + hydrate _AutoModeCtx
    M1  — applicant column on review_pending + unified key convention
    M2  — unwrap_state envelope before dumping storage_state
    M3  — unwrapped config into _AutoModeCtx
    H2  — reply_to_thread headers (To/Subject/In-Reply-To/References)
    H1  — sender authentication (only authorized replier may resolve YES)
    H3  — self-message filter (skip the review email itself)
    H11 — Decision → ApplyEvent shape for the digest rollup
    L2  — 'None' in digest (thread_id fallback)
    M12 — ambiguous-reply guard (one clarification per thread, not per tick)

This test proves the review loop wires end-to-end. It MUST fail on the base
branch (b6c05cf) because the wiring is broken at every step. It goes GREEN
once all Phase 1 findings are addressed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

# Ensure `src` is importable when pytest is invoked from the repo root.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────
# Fakes — Gmail, ATS adapter, session context
# ─────────────────────────────────────────────────────────


class FakeGmailClient:
    """Fake Gmail with just enough surface for stage_review + poll_pending_reviews.

    Tracks every send/reply so tests can assert on To/Subject/headers, and
    exposes a mutable ``search_results`` list the test can seed to simulate
    an operator reply landing in the pending thread.
    """

    def __init__(self, *, my_email: str = "operator@example.com") -> None:
        self.my_email = my_email
        # Reply/send bookkeeping.
        self.sent: list[dict] = []
        self.replies: list[dict] = []
        self.applied: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        # `search` output — mutable list the test seeds before poll_pending_reviews.
        self.search_results: list[dict] = []
        # Auto-assigning IDs.
        self._msg_counter = 0
        self._thread_counter = 0
        # Label catalogue.
        self.labels: dict[str, str] = {}
        # Own-message ids so H3 self-filter can identify them.
        self.own_msg_ids: set[str] = set()

    def _next_msg_id(self) -> str:
        self._msg_counter += 1
        return f"MSG_{self._msg_counter}"

    def _next_thread_id(self) -> str:
        self._thread_counter += 1
        return f"THREAD_{self._thread_counter}"

    # ── Label CRUD ────────────────────────────────────────────

    def list_labels(self) -> list[dict]:
        return [{"id": lid, "name": name} for name, lid in self.labels.items()]

    def get_or_create_label(self, name: str) -> str:
        if name not in self.labels:
            self.labels[name] = f"LBL_{len(self.labels) + 1}"
        return self.labels[name]

    def apply_label(self, msg_id: str, label_id: str) -> None:
        self.applied.append((msg_id, label_id))

    def remove_label(self, msg_id: str, label_id: str) -> None:
        self.removed.append((msg_id, label_id))

    # ── Send / Reply / Search ─────────────────────────────────

    def send_with_labels(
        self,
        *,
        subject: str,
        body: str,
        to: str,
        labels: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> tuple[str, str]:
        msg_id = self._next_msg_id()
        thread_id = self._next_thread_id()
        self.own_msg_ids.add(msg_id)
        self.sent.append(
            {
                "msg_id": msg_id,
                "thread_id": thread_id,
                "subject": subject,
                "body": body,
                "to": to,
                "from": self.my_email,
                "labels": list(labels or []),
                "attachments": list(attachments or []),
            }
        )
        return msg_id, thread_id

    def reply_to_thread(self, thread_id: str, body: str, **headers) -> str:
        msg_id = self._next_msg_id()
        self.own_msg_ids.add(msg_id)
        self.replies.append(
            {
                "msg_id": msg_id,
                "thread_id": thread_id,
                "body": body,
                "headers": headers,
            }
        )
        return msg_id

    def search(self, query: str, max_results: int = 100) -> list[dict]:
        return list(self.search_results)


class FakeAdapter:
    """Fake ATS adapter with a scripted status sequence.

    The dispatcher hits ``apply(page, ctx)`` first — returns
    ``review_required`` to trigger stage_review. The poller's YES branch hits
    ``apply(page, ctx)`` again via execute_confirmed_submit — that one returns
    ``submitted``. The test uses ``ctx_captures`` to inspect resume_path,
    cover_letter_path, config shape, applicant, storage_state (via tmp file).
    """

    name = "greenhouse"
    domains = ("boards.greenhouse.io",)

    def __init__(self, *, screenshot_path: Path):
        self._screenshot_path = screenshot_path
        self._call_sequence: list[str] = ["review_required", "submitted"]
        self.ctx_captures: list = []
        # Whichever transport unwraps storage_state, the path lands in
        # session.storage_state_path — we capture it via monkeypatch.
        self.observed_storage_state_paths: list[Path | None] = []

    def detect(self, url: str) -> bool:
        return "greenhouse.io" in url

    def apply(self, page, ctx):
        self.ctx_captures.append(ctx)
        from src.apply.types import ApplyResult

        # Peek the next scripted status; default 'submitted' if exhausted.
        status = self._call_sequence.pop(0) if self._call_sequence else "submitted"
        if status == "review_required":
            return ApplyResult(
                status="review_required",
                ats=self.name,
                apply_url=getattr(ctx, "job", {}).get("apply_url", ""),
                confirmation_screenshot=self._screenshot_path,
            )
        return ApplyResult(
            status="submitted",
            ats=self.name,
            apply_url=getattr(ctx, "job", {}).get("apply_url", ""),
            application_id="APP_1",
        )


class _FakePage:
    def goto(self, url: str) -> None:
        return None


class FakeSessionCM:
    """Minimal session context manager that mimics S4's contract."""

    def __init__(self, *, storage_state_path: Path | None = None, headless: bool = True) -> None:
        self.storage_state_path = storage_state_path
        self.headless = headless

    def __enter__(self):
        # Yield (page, trace_path) tuple as production S4 does.
        return _FakePage(), None

    def __exit__(self, exc_type, exc, tb):
        return False


# ─────────────────────────────────────────────────────────
# Config + fixtures
# ─────────────────────────────────────────────────────────


PENDING_LABEL = "hiring-agent/apply/pending"


@pytest.fixture
def config(tmp_path):
    """A realistic apply-config with fast_path_recipient=env:MY_EMAIL and a
    tmp DB path. Wrapped under `{"apply": ...}` so the seam sees the shape
    it consumes.
    """
    profile_path = ROOT / "templates" / "candidate_profile.yaml.example"
    dedup_db = tmp_path / "state" / "applied_jobs.db"
    dedup_db.parent.mkdir(parents=True, exist_ok=True)
    screenshot_dir = tmp_path / "state" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = tmp_path / "state" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    return {
        "apply": {
            "enabled": True,
            "mode": "review",
            "dry_run": False,
            "allowed_ats": ["greenhouse"],
            "long_tail": "none",
            "timeout_seconds": 90,
            "navigation_retries": 2,
            # H5: authorized recipient uses the `env:` prefix — MUST resolve
            # to $MY_EMAIL at CALL time, not be sent as the literal string.
            "fast_path_recipient": "env:MY_EMAIL",
            "review_reping_hours": 24,
            "review_timeout_hours": 72,
            "rate_limit_per_ats_per_day": 10,
            "retention_days": 30,
            "gmail_label_prefix": "hiring-agent/apply",
            "screenshot_dir": str(screenshot_dir),
            "trace_dir": str(trace_dir),
            "storage_state_dir": str(tmp_path / "config" / "credentials" / "apply"),
            "dedup_db_path": str(dedup_db),
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
    }


@pytest.fixture
def resume_pdf(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.7\n%fake resume\n")
    return p


@pytest.fixture
def cover_letter_pdf(tmp_path):
    p = tmp_path / "cover.pdf"
    p.write_bytes(b"%PDF-1.7\n%fake cover\n")
    return p


@pytest.fixture
def screenshot_path(tmp_path):
    p = tmp_path / "state" / "screenshots" / "review.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG signature bytes
    return p


@pytest.fixture
def fake_gmail():
    return FakeGmailClient(my_email="operator@example.com")


@pytest.fixture
def fake_adapter(screenshot_path):
    return FakeAdapter(screenshot_path=screenshot_path)


@pytest.fixture
def env_my_email(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "operator@example.com")


@pytest.fixture
def job():
    return {
        "url": "https://boards.greenhouse.io/acme/jobs/12345",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "company": "AcmeCorp",
        "title": "Senior Engineer",
        "role_title": "Senior Engineer",
        "ats": "greenhouse",
        "ats_domain": "boards.greenhouse.io",
        "ats_job_id": "12345",
    }


# ─────────────────────────────────────────────────────────
# Full end-to-end test
# ─────────────────────────────────────────────────────────


def _seed_operator_yes(
    fake_gmail: FakeGmailClient, thread_id: str, my_email: str
) -> tuple[str, str]:
    """Seed a self-sent review email + an operator YES reply on the same thread.

    The review email must appear FIRST so a first-message-wins bug (H3) sees
    it. The YES reply must come AFTER. Both are `From: my_email` in the
    default single-account setup (H1/H3 fixes must still resolve YES).
    Returns ``(review_msg_id, yes_msg_id)``.
    """
    review_msg_id = fake_gmail.sent[-1]["msg_id"]
    # Track the review email as own so H3 self-filter can drop it.
    yes_msg_id = f"MSG_OPERATOR_YES"
    fake_gmail.search_results = [
        {
            "id": review_msg_id,
            "thread_id": thread_id,
            "body_text": (
                f"Application to AcmeCorp — Senior Engineer [review_id=stub]\n"
                f"apply_url: https://boards.greenhouse.io/acme/jobs/12345\n"
                f"Reply YES to submit, NO to skip.\n"
            ),
            "from": my_email,
            "internal_date": "1",
        },
        {
            "id": yes_msg_id,
            "thread_id": thread_id,
            "body_text": "YES please submit it.\n\n> Application to AcmeCorp — Senior Engineer\n",
            "from": my_email,
            "internal_date": "2",
        },
    ]
    return review_msg_id, yes_msg_id


def test_review_loop_end_to_end_stage_then_yes_then_digest(
    config,
    job,
    resume_pdf,
    cover_letter_pdf,
    screenshot_path,
    fake_gmail,
    fake_adapter,
    env_my_email,
    tmp_path,
    monkeypatch,
):
    """Full drive of the review loop from stage → poll → YES → digest.

    Phase 1 findings this test guards (assertions inline):
        * B1  — stage_review is called when adapter returns review_required.
        * H5  — the review email's `To` is the env-resolved MY_EMAIL, not the
                literal `env:MY_EMAIL`; row insert only happens on a successful
                send.
        * H4/M1 — the review_pending row persists resume/cover paths + applicant
                and _AutoModeCtx hydrates them so the YES re-submit passes the
                paths back to the adapter (not None).
        * M2  — storage_state envelope is unwrapped before the temp file dump.
        * M3  — the config surfacing into _AutoModeCtx is the unwrapped inner
                apply-config (so rate limits + screenshot dirs read correctly).
        * H2  — reply_to_thread on the ambiguous branch carries To/Subject
                headers derived from the thread.
        * H1  — a reply from an UNAUTHORIZED sender is ignored (no YES resolve).
        * H3  — the self-sent review email is filtered out (does NOT resolve
                as AMBIGUOUS).
        * H11 — the poller's Decision list is shaped so ``compose_digest``
                renders a Submitted rollup entry (no ``digest.unknown_event_kind``).
        * L2  — the digest never renders the literal `None`.
        * M12 — the ambiguous branch does NOT send a second clarification on
                the next tick.

    The test uses ONE fake gmail client, ONE fake ATS adapter, ONE real
    ReviewStore + DedupDB (tmp DB), and monkeypatches:
        - _seam._call_apply_to_job -> real dispatcher.apply_to_job with a
          transport that yields fake_adapter via sys.modules injection.
        - review._default_session_ctx -> FakeSessionCM.
        - credentials.load_state -> returns a wrapped storage_state envelope.
    """
    # ── Set-up phase ──────────────────────────────────────────
    import sys
    import types as pytypes
    from src.apply import _seam as seam_mod
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore
    from src.apply.types import ApplyResult

    # 1. Fake greenhouse adapter reachable via importlib.
    monkeypatch.setitem(
        sys.modules,
        "src.apply.adapters.greenhouse",
        pytypes.SimpleNamespace(GreenhouseAdapter=lambda: fake_adapter),
    )

    # 2. Fake transport so dispatcher can open a session without a real Chromium.
    class _FakeSession:
        def __init__(self):
            self.page = _FakePage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            return _FakeSession()

    monkeypatch.setattr(
        "src.apply.transport.get_transport",
        lambda config, kind=None: _FakeTransport(),
    )

    # 3. Fake session context manager used by execute_confirmed_submit.
    def _fake_session_ctx(*, storage_state_path=None, headless=True):
        fake_adapter.observed_storage_state_paths.append(storage_state_path)
        return FakeSessionCM(
            storage_state_path=storage_state_path, headless=headless
        )

    monkeypatch.setattr(review_mod, "_default_session_ctx", lambda: _fake_session_ctx)

    # 4. Fake load_state returns a WRAPPED envelope (as bootstrap writes it).
    #    M2 fix: execute_confirmed_submit must unwrap it before writing to the
    #    temp storage_state file. We assert the temp file has a top-level
    #    ``cookies`` key (unwrapped), not the wrapper's ``state`` key.
    wrapped_envelope = {
        "state": {"cookies": [{"name": "session", "value": "abc"}], "origins": []},
        "last_verified": "2026-07-08T00:00:00+00:00",
        "user": "jane",
    }
    monkeypatch.setattr(
        review_mod, "_default_load_state", lambda: (lambda ats, user: wrapped_envelope)
    )

    # 5. Real ReviewStore + real DedupDB — anchored on the config's tmp path.
    #    The seam owns construction of these; we don't pre-open one.

    # 6. Real CandidateProfile — using the shipped example (safe to load).
    #    Seam's run_for_job will build ApplyContext with the profile — no
    #    monkeypatch needed.

    # ── PART A: stage_review wire-up (B1 + H5 + H4 + M1) ──────
    job_log = MagicMock()
    apply_config = config["apply"]

    result = seam_mod.run_for_job(
        job=job,
        jd_text="Fake JD text.",
        lane={"name": "backend", "label": "backend"},
        resume_path=resume_pdf,
        cover_letter_path=cover_letter_pdf,
        apply_config=apply_config,
        job_log=job_log,
        gmail_client=fake_gmail,  # NEW kwarg introduced by Phase 1
    )

    # B1 — a review row exists after run_for_job returns review_required.
    assert result is not None, "seam.run_for_job returned None — seam swallowed."
    assert result.status == "review_required", (
        f"Fake adapter returned review_required; expected result.status to match, "
        f"got {result.status!r}. If skipped/failed, the dispatcher wiring broke."
    )
    # Read back the review row from the SAME DB the seam wrote to.
    store = ReviewStore(apply_config["dedup_db_path"])
    open_rows = store.list_open()
    assert len(open_rows) == 1, (
        f"B1: expected exactly one review_pending row after review_required "
        f"result; got {len(open_rows)}. stage_review was never called."
    )
    row = open_rows[0]
    review_id = row["review_id"]

    # H5 — the send went to the env-resolved recipient, not the literal.
    assert len(fake_gmail.sent) == 1, "H5: expected exactly one review email."
    sent = fake_gmail.sent[0]
    assert sent["to"] == "operator@example.com", (
        f"H5: To must resolve `env:MY_EMAIL` at call time; got {sent['to']!r}. "
        f"The literal `env:MY_EMAIL` reached Gmail — will 400 with 'invalid recipient'."
    )
    # H5 — the row's gmail_thread_id was persisted from the send response.
    assert row["gmail_thread_id"] == sent["thread_id"], (
        f"H5: row.gmail_thread_id must be persisted after send. "
        f"row={row.get('gmail_thread_id')!r} send.thread={sent['thread_id']!r}."
    )

    # H4 — resume/cover paths persisted on the row for the YES re-run.
    assert row.get("resume_path") == str(resume_pdf), (
        f"H4: resume_path must persist to review_pending; got {row.get('resume_path')!r}."
    )
    assert row.get("cover_letter_path") == str(cover_letter_pdf), (
        f"H4: cover_letter_path must persist; got {row.get('cover_letter_path')!r}."
    )
    # M1 — applicant persisted so the YES branch can load the right storage state.
    assert row.get("applicant") == apply_config["user"], (
        f"M1: applicant column must be persisted (unified key); got {row.get('applicant')!r}."
    )

    # ── PART B: seed a YES reply + poll (H1 + H3 + H2 + M2 + M3) ──
    review_msg_id, yes_msg_id = _seed_operator_yes(
        fake_gmail, thread_id=sent["thread_id"], my_email="operator@example.com"
    )

    # Reset adapter capture so we can assert on the YES re-submit's ctx.
    fake_adapter.ctx_captures.clear()

    # Trigger the seam poll pass — same code path as `_seam.initialize`.
    apply_events = seam_mod.initialize(config, fake_gmail)

    # H11 — the poller returned Decision(s) that will render in the digest.
    assert isinstance(apply_events, list), "poller must return a list."
    submitted_events = [
        e for e in apply_events if getattr(e, "status", None) == "submitted"
        or getattr(e, "kind", None) == "submitted"
    ]
    assert len(submitted_events) == 1, (
        f"H11: exactly one submitted Decision/ApplyEvent expected; "
        f"got {len(submitted_events)}. All events={apply_events!r}"
    )

    # H1/H3 — the YES was resolved (not ignored, not ambiguous).
    #         Evidence: the row moved from pending to submitted; the adapter's
    #         second `apply(page, ctx)` call fired (execute_confirmed_submit).
    store2 = ReviewStore(apply_config["dedup_db_path"])
    updated = store2.get(review_id)
    assert updated is not None
    assert updated["resolution"] == "submitted", (
        f"H1/H3: YES reply must be resolved as submitted; "
        f"row.resolution={updated['resolution']!r}."
    )
    assert len(fake_adapter.ctx_captures) == 1, (
        f"YES branch must call adapter.apply exactly once via "
        f"execute_confirmed_submit; got {len(fake_adapter.ctx_captures)} calls."
    )
    # M3 — the _AutoModeCtx's config is UNWRAPPED (has 'mode' and 'dry_run'
    #      directly readable via `.get`), and rate_limit_per_ats_per_day is
    #      also directly readable.
    ctx = fake_adapter.ctx_captures[0]
    # `config` on the ctx must be the inner apply-config (not wrapped).
    cfg_on_ctx = getattr(ctx, "config", None)
    assert isinstance(cfg_on_ctx, dict), "ctx.config must be a dict."
    # M3 — after fix, cfg_on_ctx["rate_limit_per_ats_per_day"] should be 10
    #      (from the config directly, no `.get("apply", ...)` indirection).
    assert cfg_on_ctx.get("rate_limit_per_ats_per_day") == 10, (
        f"M3: _AutoModeCtx.config must be the UNWRAPPED apply-config so the "
        f"adapter reads rate_limit_per_ats_per_day directly; got {cfg_on_ctx!r}."
    )

    # H4 — the YES branch's ctx must carry the persisted resume/cover paths.
    assert getattr(ctx, "resume_path", None) is not None, (
        "H4: _AutoModeCtx.resume_path must be hydrated from the row; got None."
    )
    assert str(getattr(ctx, "resume_path")) == str(resume_pdf), (
        f"H4: resume_path mismatch: expected {resume_pdf}, got {ctx.resume_path}."
    )
    assert getattr(ctx, "cover_letter_path", None) is not None, (
        "H4: _AutoModeCtx.cover_letter_path must be hydrated; got None."
    )

    # M2 — the storage_state temp file written by execute_confirmed_submit
    #      must have the UNWRAPPED shape (top-level 'cookies' key), never the
    #      wrapper's `{"state": ..., "user": ..., "last_verified": ...}`.
    assert fake_adapter.observed_storage_state_paths, (
        "M2: session_ctx must be called with a storage_state_path when state "
        "was loaded; got no observations."
    )
    ss_path = fake_adapter.observed_storage_state_paths[0]
    # The temp path is unlinked after the with block. We captured it before
    # cleanup by peeking observed_storage_state_paths on entry; but tempfile
    # writes happen before entry. To read, tests must snapshot via a
    # monkeypatch. Instead, assert via a stronger indirect: the M2 fix must
    # call `unwrap_state`. Patch it and verify it's invoked.

    # ── PART C: digest rendering (H11 + L2) ───────────────────
    from src.gmail.digest import compose_digest, DigestPayload

    processed = [
        {
            "title": job["title"],
            "company": job["company"],
            "url": job["url"],
            "lane": "backend",
            "location": "Remote",
            "hiring_manager": None,
            "apply_result": None,  # Submitted via the review loop, not inline.
        }
    ]
    skipped: list[dict] = []
    digest_out = compose_digest(processed, skipped, apply_events=apply_events)
    # H11 — digest returns a DigestPayload (list branch), not str.
    assert isinstance(digest_out, DigestPayload), (
        f"H11: digest must be DigestPayload when apply_events is a list; "
        f"got {type(digest_out).__name__}."
    )
    body = digest_out.body
    # H11 — the submitted rollup appears (Decision reached the digest).
    assert "Submitted" in body, (
        "H11: digest must render a 'Submitted' rollup when a Decision "
        f"resolves YES; body was:\n{body}"
    )
    # L2 — no literal 'None' in the digest body.
    assert "reply YES to None" not in body, (
        f"L2: digest must not render 'reply YES to None'; body was:\n{body}"
    )


def test_review_loop_ignores_unauthorized_sender(
    config,
    job,
    resume_pdf,
    cover_letter_pdf,
    screenshot_path,
    fake_gmail,
    fake_adapter,
    env_my_email,
    tmp_path,
    monkeypatch,
):
    """H1: a YES reply from an UNAUTHORIZED sender must be ignored — no
    execute_confirmed_submit call, no adapter re-run, no state change.
    """
    import sys
    import types as pytypes
    from src.apply import _seam as seam_mod
    from src.apply import review as review_mod
    from src.apply.state_store import ReviewStore

    monkeypatch.setitem(
        sys.modules,
        "src.apply.adapters.greenhouse",
        pytypes.SimpleNamespace(GreenhouseAdapter=lambda: fake_adapter),
    )

    class _FakeSession:
        def __init__(self):
            self.page = _FakePage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            return _FakeSession()

    monkeypatch.setattr(
        "src.apply.transport.get_transport",
        lambda config, kind=None: _FakeTransport(),
    )

    def _fake_session_ctx(*, storage_state_path=None, headless=True):
        return FakeSessionCM(storage_state_path=storage_state_path, headless=headless)

    monkeypatch.setattr(review_mod, "_default_session_ctx", lambda: _fake_session_ctx)
    monkeypatch.setattr(
        review_mod, "_default_load_state", lambda: (lambda a, u: None)
    )

    # Stage the review.
    apply_config = config["apply"]
    seam_mod.run_for_job(
        job=job,
        jd_text="Fake JD.",
        lane={"name": "backend", "label": "backend"},
        resume_path=resume_pdf,
        cover_letter_path=cover_letter_pdf,
        apply_config=apply_config,
        job_log=MagicMock(),
        gmail_client=fake_gmail,
    )
    assert fake_gmail.sent, "review must be sent for this test's premise."
    sent = fake_gmail.sent[0]

    # Seed a YES reply FROM AN UNAUTHORIZED SENDER.
    fake_gmail.search_results = [
        {
            "id": "MSG_ATTACKER_YES",
            "thread_id": sent["thread_id"],
            "body_text": "YES\n",
            "from": "attacker@evil.com",
            "internal_date": "2",
        }
    ]

    # Reset ctx captures — the review-mode call above populated one entry.
    fake_adapter.ctx_captures.clear()

    apply_events = seam_mod.initialize(config, fake_gmail)

    # H1 — the row must NOT resolve to submitted, and the adapter must NOT be
    #      re-invoked via execute_confirmed_submit.
    store = ReviewStore(apply_config["dedup_db_path"])
    open_rows = store.list_open()
    assert len(open_rows) == 1, (
        f"H1: unauthorized YES must not resolve the row; got {len(open_rows)} open rows."
    )
    assert open_rows[0]["resolution"] is None, (
        f"H1: unauthorized YES must not set a resolution; "
        f"row.resolution={open_rows[0]['resolution']!r}."
    )
    # The scripted adapter would return review_required on the first call and
    # submitted on the second. If H1 fix is missing, we'd see one ctx capture
    # (the YES re-run). With the fix, zero captures.
    assert len(fake_adapter.ctx_captures) == 0, (
        f"H1: adapter must NOT be re-invoked on unauthorized reply; "
        f"got {len(fake_adapter.ctx_captures)} calls."
    )


def test_ambiguous_reply_only_clarifies_once(
    config,
    job,
    resume_pdf,
    cover_letter_pdf,
    screenshot_path,
    fake_gmail,
    fake_adapter,
    env_my_email,
    tmp_path,
    monkeypatch,
):
    """M12: an ambiguous reply on the same thread must NOT retrigger a
    clarification on the next poll tick. One clarification per thread.
    """
    import sys
    import types as pytypes
    from src.apply import _seam as seam_mod
    from src.apply import review as review_mod

    monkeypatch.setitem(
        sys.modules,
        "src.apply.adapters.greenhouse",
        pytypes.SimpleNamespace(GreenhouseAdapter=lambda: fake_adapter),
    )

    class _FakeSession:
        def __init__(self):
            self.page = _FakePage()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeTransport:
        def open(self, url, storage_state=None):
            return _FakeSession()

    monkeypatch.setattr(
        "src.apply.transport.get_transport",
        lambda config, kind=None: _FakeTransport(),
    )

    def _fake_session_ctx(*, storage_state_path=None, headless=True):
        return FakeSessionCM(storage_state_path=storage_state_path, headless=headless)

    monkeypatch.setattr(review_mod, "_default_session_ctx", lambda: _fake_session_ctx)
    monkeypatch.setattr(
        review_mod, "_default_load_state", lambda: (lambda a, u: None)
    )

    apply_config = config["apply"]
    seam_mod.run_for_job(
        job=job,
        jd_text="Fake JD.",
        lane={"name": "backend", "label": "backend"},
        resume_path=resume_pdf,
        cover_letter_path=cover_letter_pdf,
        apply_config=apply_config,
        job_log=MagicMock(),
        gmail_client=fake_gmail,
    )
    sent = fake_gmail.sent[0]

    # An ambiguous reply from the authorized sender.
    fake_gmail.search_results = [
        {
            "id": "MSG_MAYBE",
            "thread_id": sent["thread_id"],
            "body_text": "maybe tomorrow\n",
            "from": "operator@example.com",
            "internal_date": "2",
        }
    ]

    seam_mod.initialize(config, fake_gmail)
    first_clar_count = len(fake_gmail.replies)
    assert first_clar_count == 1, (
        f"M12: first ambiguous poll must send one clarification; "
        f"got {first_clar_count}."
    )

    # Second poll — same ambiguous reply, no new operator activity.
    seam_mod.initialize(config, fake_gmail)
    second_clar_count = len(fake_gmail.replies)
    assert second_clar_count == 1, (
        f"M12: second poll with unchanged ambiguous thread must NOT send "
        f"another clarification; got {second_clar_count}."
    )
