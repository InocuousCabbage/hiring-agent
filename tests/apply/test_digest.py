"""S14 digest-integration tests — RED first, GREEN after.

Every test in this file corresponds to a bullet in spec §TDD scaffolding.
The block-render tests exercise the five ``ApplyEvent`` kinds one at a time
plus a stability check for the fixed section order.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Bootstrap: match tests/test_review_fixes.py convention so ``from gmail.digest``
# and ``from apply.types`` resolve without a package rename.
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Also make ``tests.fixtures.apply`` importable directly.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gmail.digest import DigestPayload, compose_digest  # noqa: E402
from tests.fixtures.apply.digest_events import (  # noqa: E402
    make_auto_declined_event,
    make_bootstrap_needed_event,
    make_review_required_event,
    make_soft_dup_event,
    make_submitted_event,
)


# ---------------------------------------------------------------------------
# Fixtures — the "legacy" processed/skipped shape that has to remain byte-
# identical when no ``apply_events`` are handed in.
# ---------------------------------------------------------------------------

LEGACY_PROCESSED = [
    {
        "title": "Senior Software Engineer",
        "company": "Acme Corp",
        "location": "Remote",
        "lane": "linkedin_search",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
    },
    {
        "title": "Staff Engineer",
        "company": "Beta LLC",
        "location": "Portland, ME",
        "lane": "company_page",
        "url": "https://jobs.beta.com/staff-engineer",
        "hiring_manager": {
            "name": "Jane Doe",
            "title": "VP Engineering",
            "confidence": "high",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "email": "jane@beta.com",
            "outreach_note": "Referred by peer.",
        },
    },
]

LEGACY_SKIPPED = [
    {
        "title": "Junior Dev",
        "company": "Gamma Inc",
        "url": "https://jobs.gamma.com/junior",
        "reason": "below_threshold",
    },
]


@pytest.fixture
def golden_body() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "apply"
        / "digest_golden.txt"
    ).read_text()


# ---------------------------------------------------------------------------
# 1. Back-compat: no kwarg -> str, byte-identical to golden.
# ---------------------------------------------------------------------------


def test_back_compat_returns_string_when_no_apply_kwarg(golden_body: str) -> None:
    out = compose_digest(LEGACY_PROCESSED, LEGACY_SKIPPED)
    assert isinstance(out, str)
    assert out == golden_body


def test_back_compat_returns_string_when_no_apply_results_in_processed(
    golden_body: str,
) -> None:
    # Same golden even when the kwarg is present but explicitly ``None``.
    out = compose_digest(LEGACY_PROCESSED, LEGACY_SKIPPED, apply_events=None)
    assert isinstance(out, str)
    assert out == golden_body


# ---------------------------------------------------------------------------
# 2. Kwarg-presence branch — empty list still yields DigestPayload.
# ---------------------------------------------------------------------------


def test_returns_payload_when_apply_events_kwarg_passed_empty(
    golden_body: str,
) -> None:
    out = compose_digest(LEGACY_PROCESSED, LEGACY_SKIPPED, apply_events=[])
    assert isinstance(out, DigestPayload)
    assert out.body == golden_body
    assert out.attachments == []


# ---------------------------------------------------------------------------
# 3. Per-block renderers.
# ---------------------------------------------------------------------------


def test_renders_submitted_block() -> None:
    ev = make_submitted_event(ats="greenhouse", application_id="12345")
    payload = compose_digest([], [], apply_events=[ev])
    assert isinstance(payload, DigestPayload)
    assert "## Submitted" in payload.body
    assert "Submitted to greenhouse — application_id 12345" in payload.body


def test_renders_review_required_block_and_attaches_png(tmp_path: Path) -> None:
    png = tmp_path / "confirmation.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal valid-ish PNG header

    ev = make_review_required_event(
        ats="greenhouse",
        gmail_thread_id="thread-42",
        review_id="rev-42",
        screenshot_path=str(png),
    )
    payload = compose_digest([], [], apply_events=[ev])
    assert isinstance(payload, DigestPayload)
    assert "## Review required" in payload.body
    assert "reply YES to thread-42" in payload.body
    assert png.resolve() in [p.resolve() for p in payload.attachments]


def test_review_required_missing_png_drops_attachment_silently(
    tmp_path: Path, caplog
) -> None:
    ghost = tmp_path / "does_not_exist.png"

    ev = make_review_required_event(
        gmail_thread_id="thread-42",
        review_id="rev-42",
        screenshot_path=str(ghost),
    )
    with caplog.at_level("INFO"):
        payload = compose_digest([], [], apply_events=[ev])

    assert isinstance(payload, DigestPayload)
    assert "reply YES to thread-42" in payload.body
    assert payload.attachments == []
    # The log must reference the review_id, never the ghost path.
    joined_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "digest.screenshot_missing" in joined_logs
    assert "rev-42" in joined_logs
    assert str(ghost) not in joined_logs


def test_renders_auto_declined_block() -> None:
    ev = make_auto_declined_event(ats="greenhouse", review_id="rev-old")
    payload = compose_digest([], [], apply_events=[ev])
    assert isinstance(payload, DigestPayload)
    assert "## Auto-declined" in payload.body
    assert "no reply in 72 h" in payload.body


def test_renders_soft_dup_block() -> None:
    ev = make_soft_dup_event(
        company="Acme", similar_role="Senior Engineer", review_id="rev-dup"
    )
    payload = compose_digest([], [], apply_events=[ev])
    assert isinstance(payload, DigestPayload)
    assert "Blocked (soft-dup) — similar role at Acme" in payload.body
    assert "reply YES rev-dup to override" in payload.body


def test_renders_bootstrap_needed_deduped_by_ats() -> None:
    evs = [
        make_bootstrap_needed_event(ats="greenhouse"),
        make_bootstrap_needed_event(ats="greenhouse"),
        make_bootstrap_needed_event(ats="greenhouse"),
    ]
    payload = compose_digest([], [], apply_events=evs)
    assert isinstance(payload, DigestPayload)
    # Exactly one bullet.
    assert payload.body.count("Bootstrap needed — greenhouse session expired") == 1


# ---------------------------------------------------------------------------
# 4. PII guard.
# ---------------------------------------------------------------------------


def test_no_pii_leaks_into_body(tmp_path: Path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    evs = [
        make_submitted_event(),
        make_review_required_event(screenshot_path=str(png)),
        make_auto_declined_event(),
        make_soft_dup_event(),
        make_bootstrap_needed_event(),
    ]
    payload = compose_digest([], [], apply_events=evs)
    assert isinstance(payload, DigestPayload)
    body = payload.body
    for pii in (
        "secret@example.com",
        "Ben",
        "Joslin",
        "+1-555-0100",
        "https://linkedin.com/in/secret",
    ):
        assert pii not in body, f"PII leak: {pii!r} appeared in body"
    # And no candidate PII in attachment filenames either (spec BLOCKING).
    for att in payload.attachments:
        name = att.name
        for pii in ("secret", "Ben", "Joslin"):
            assert pii not in name, f"PII in attachment filename: {name}"


# ---------------------------------------------------------------------------
# 5. Attachment dedup by absolute path.
# ---------------------------------------------------------------------------


def test_attachment_dedup_by_absolute_path(tmp_path: Path) -> None:
    png = tmp_path / "one.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    ev1 = make_review_required_event(
        review_id="rev-1",
        gmail_thread_id="t-1",
        screenshot_path=str(png),
    )
    ev2 = make_review_required_event(
        review_id="rev-2",
        gmail_thread_id="t-2",
        screenshot_path=str(png),  # SAME path, upstream double-add.
    )
    payload = compose_digest([], [], apply_events=[ev1, ev2])
    assert isinstance(payload, DigestPayload)
    resolved = {p.resolve() for p in payload.attachments}
    assert len(resolved) == 1
    assert len(payload.attachments) == 1


# ---------------------------------------------------------------------------
# 6. Block order stability.
# ---------------------------------------------------------------------------


def test_block_order_is_stable(tmp_path: Path) -> None:
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    # Pass in intentionally scrambled order.
    evs = [
        make_bootstrap_needed_event(ats="greenhouse"),
        make_soft_dup_event(company="Acme", review_id="r1"),
        make_submitted_event(ats="greenhouse", application_id="9"),
        make_auto_declined_event(review_id="r-old"),
        make_review_required_event(
            gmail_thread_id="t-1", review_id="r-2", screenshot_path=str(png)
        ),
    ]
    payload = compose_digest([], [], apply_events=evs)
    body = payload.body
    ordered_headers = [
        "## Submitted",
        "## Review required",
        "## Auto-declined",
        "## Blocked (soft-dup)",
        "## Bootstrap needed",
    ]
    positions = [body.find(h) for h in ordered_headers]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), (
        f"Block order drift: {list(zip(ordered_headers, positions))}"
    )


# ---------------------------------------------------------------------------
# 7. Landmine L6 — no datetime.utcnow.
# ---------------------------------------------------------------------------


def test_no_utcnow() -> None:
    src = (_SRC / "gmail" / "digest.py").read_text()
    assert "utcnow" not in src, "L6 violation: datetime.utcnow() found in digest.py"


# ---------------------------------------------------------------------------
# 8. Unknown-kind event is skipped, not raised.
# ---------------------------------------------------------------------------


def test_unknown_event_kind_is_skipped(caplog) -> None:
    from apply.types import ApplyEvent  # local import — S2 contract

    # Bypass frozen constraint by constructing with a bogus literal.
    ev = ApplyEvent.__new__(ApplyEvent)
    object.__setattr__(ev, "kind", "totally_bogus")
    object.__setattr__(ev, "row", {"ats": "greenhouse"})

    with caplog.at_level("INFO"):
        payload = compose_digest([], [], apply_events=[ev])
    assert isinstance(payload, DigestPayload)
    joined_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "digest.unknown_event_kind" in joined_logs
