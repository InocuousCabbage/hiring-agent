"""
gmail/digest.py — Compose and send the summary digest email.

S14 extension: `compose_digest` grew a keyword-only ``apply_events`` argument.
- Absent kwarg  -> returns ``str`` (byte-identical to the pre-S14 output).
- Present kwarg (even an empty list) -> returns a ``DigestPayload`` namedtuple
  ``(body, attachments)`` so the S17 seam can attach confirmation PNGs.

The five rollup blocks render in a fixed order, each only when at least one
row exists:
    1. Submitted
    2. Review required   (attaches confirmation PNG per row when present)
    3. Auto-declined
    4. Blocked (soft-dup)
    5. Bootstrap needed  (deduped by ATS)

Landmines honored:
- L6: timestamps go through ``datetime.now(tz=UTC)`` — the deprecated
  naive-UTC builder is banned by a source-grep in tests/apply/test_digest.py.
- L7: candidate PII (email, phone, first/last name, address, linkedin_url,
  answer text) is never rendered into the body or attachment filenames. The
  per-block renderers read only the whitelisted keys the spec calls out.
"""

from __future__ import annotations

import logging
from collections import namedtuple
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

DigestPayload = namedtuple("DigestPayload", ["body", "attachments"])

# Fixed order for the rollup blocks (spec §Acceptance criterion #3, #4).
#
# xhigh-BLOCKING/H3: `submitted_unrecorded` renders BETWEEN Submitted and
# Review-required so operators see the double-submit-risk warning right
# after the successful submissions — high visibility for the escalation.
_BLOCK_ORDER: tuple[str, ...] = (
    "submitted",
    "submitted_unrecorded",
    "review_required",
    "auto_declined",
    "soft_dup",
    "bootstrap_needed",
)


def compose_digest(
    processed: list[dict],
    skipped: list[dict],
    attachments: "list | None" = None,
    *,
    apply_events: "list[Any] | None" = None,
) -> "str | DigestPayload":
    """Build the digest.

    Return-type contract (spec §Acceptance criterion #5 + tests):
        - ``apply_events`` unset or ``None`` -> plain ``str`` (pre-S14 shape).
        - ``apply_events`` is a list (even ``[]``) -> ``DigestPayload``.

    ``attachments`` (positional or kwarg) is the pre-S14 origin/main hook for
    the dual-output renderer; when provided we prepend a PDF-or-DOCX note to
    the body via Path.suffix inspection (canonical, matches other codepaths).
    """
    body = _render_legacy_body(processed, skipped, attachments=attachments)

    # Legacy path — no apply pipeline in play.
    if apply_events is None:
        return body

    events: list[Any] = list(apply_events)
    rollup_text, rollup_attachments = _render_apply_rollup(events, processed)
    if rollup_text:
        # Insert rollup BEFORE the "— Hiring Agent (automated)" sign-off so the
        # signature stays last. Slice on the exact tail we appended.
        sign_off = "\n\n— Hiring Agent (automated)"
        if body.endswith(sign_off):
            body = body[: -len(sign_off)] + "\n" + rollup_text + sign_off
        else:  # defensive: keep rollup before whatever tail exists
            body = body + "\n" + rollup_text
    return DigestPayload(body=body, attachments=rollup_attachments)


# ---------------------------------------------------------------------------
# Legacy body — kept 1:1 with the pre-S14 output so the golden test locks it.
# ---------------------------------------------------------------------------


def _render_legacy_body(
    processed: list[dict],
    skipped: list[dict],
    *,
    attachments: "list | None" = None,
) -> str:
    lines: list[str] = []

    # Origin/main dual-output attachment note (checked via Path.suffix — canonical).
    if attachments:
        suffixes = {Path(p).suffix.lower() for p in attachments}
        has_pdf = ".pdf" in suffixes
        has_docx = ".docx" in suffixes
        if has_pdf and has_docx:
            lines.append(
                "Both PDF (for direct submission) and editable DOCX "
                "(for last-minute edits in Word/Google Docs) are attached."
            )
            lines.append("")
        elif has_docx and not has_pdf:
            lines.append(
                "Editable DOCX attached (for last-minute edits in "
                "Word/Google Docs) — no PDF converter is installed, so the "
                "PDF is missing. Install LibreOffice on the agent box to "
                "restore the PDF + DOCX pair."
            )
            lines.append("")

    lines.append(f"Processed ({len(processed)})")
    lines.append("=" * 40)
    for job in processed:
        # PR #12 finding #11 (altitude-fix scope-out sweep): parser
        # returns `location=""` for trailing-dash raws like `"Acme —"`.
        # `.get("location", "Unknown")` returns `""` (not the default)
        # when the key exists with an empty value — same class as the
        # company-side sentinel PR #11 fixed. `.get(...) or default`
        # coerces both empty-string and None to the fallback.
        location = job.get("location") or "Unknown"
        lines.append(f"  {job['title']} — {job['company']} ({location})")
        # PR #12 iter-2 sweep: same L2 class as location/hm fields. `lane`
        # is populated by main.py from `lane["label"]` (config-side); an
        # empty label from a misconfigured lane row leaks a blank
        # 'Lane: ' line pre-sweep. `.get(...) or default` is belt-and-
        # suspenders defense against the empty-string branch.
        lines.append(f"  Lane: {job.get('lane') or 'N/A'}")
        lines.append(f"  URL: {job['url']}")
        hm = job.get("hiring_manager")
        if hm:
            # PR #12 L2-class sweep: `.get(k, default)` returns `""` when
            # the key exists with an empty value. LLM-emitted `hm` dict
            # can carry `""` on partial-confidence matches — coerce to
            # the human-readable fallback via `.get(...) or default`.
            # Constraint: hm['confidence'] is str enum {high, medium, low}
            # per hm_finder.py:166 — a future numeric-confidence migration
            # would need to narrow this sweep to `is not None`.
            lines.append(
                f"  Hiring Manager: {hm.get('name') or 'Unknown'} — "
                f"{hm.get('title') or 'N/A'} ({hm.get('confidence') or 'N/A'})"
            )
            if hm.get("linkedin_url"):
                lines.append(f"  LinkedIn: {hm['linkedin_url']}")
            if hm.get("email"):
                lines.append(f"  Email: {hm['email']}")
            if hm.get("outreach_note"):
                lines.append(f"  Outreach: {hm['outreach_note']}")
        lines.append("")

    if skipped:
        lines.append(f"\nSkipped ({len(skipped)})")
        lines.append("=" * 40)
        for job in skipped:
            lines.append(f"  {job['title']} — {job['company']}")
            lines.append(f"  URL: {job['url']}")
            # PR #12 iter-2 sweep — same L2 class. `reason` is code-
            # generated in main.py but an f-string with empty substitution
            # (e.g. `f"Poor fit — confidence /100"` when confidence is None)
            # would leak a partial-blank tail.
            lines.append(f"  Reason: {job.get('reason') or 'Unknown'}")
            lines.append("")

    lines.append("\n— Hiring Agent (automated)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply rollup — five blocks + attachment collection.
# ---------------------------------------------------------------------------


def _render_apply_rollup(
    events: list[Any],
    processed_apply_results: Iterable[dict],
) -> tuple[str, list[Path]]:
    """Return ``(rollup_text, attachments)``.

    ``processed_apply_results`` is scanned for ``apply_result`` fields the
    S17 seam will attach to each processed job. When neither the events list
    nor the processed apply_results contain anything renderable, returns
    ``("", [])`` so the caller can skip the rollup section entirely.
    """
    buckets: dict[str, list[dict]] = {kind: [] for kind in _BLOCK_ORDER}

    for ev in events:
        # H11 fix: the review poller returns `Decision` (frozen dataclass with
        # `.status` + flat fields), NOT `ApplyEvent(kind, row)`. Previously
        # this loop only read `.kind` → miss → `digest.unknown_event_kind` →
        # every submitted/auto_declined resolution was dropped from the
        # digest. Now we accept both shapes.
        kind = getattr(ev, "kind", None)
        row: dict
        if kind is None:
            # Decision shape — synthesize a bucket kind from `.status`.
            status = getattr(ev, "status", None)
            kind = _decision_status_to_bucket(status)
            row = _decision_to_row(ev)
        else:
            row = getattr(ev, "row", {}) or {}

        if kind not in buckets:
            _log.info("digest.unknown_event_kind kind=%s", kind)
            continue
        buckets[kind].append(row)

    # ``processed_apply_results`` are ApplyResult objects hanging off processed
    # jobs (seam contract §2). Merge them into the same buckets so the rollup
    # doesn't miss a submitted-in-line job.
    for job in processed_apply_results or []:
        ar = job.get("apply_result") if isinstance(job, dict) else None
        if ar is None:
            continue
        status = _read_status(ar)
        row = _apply_result_to_row(ar)
        if status == "submitted":
            buckets["submitted"].append(row)
        elif status == "submitted_unrecorded":
            # xhigh-BLOCKING/H3
            buckets["submitted_unrecorded"].append(row)
        elif status == "review_required":
            buckets["review_required"].append(row)
        elif status == "auto_declined":
            buckets["auto_declined"].append(row)
        elif status == "soft_dup_warn":
            buckets["soft_dup"].append(row)
        elif status == "skipped" and row.get("reason") == "session_expired":
            buckets["bootstrap_needed"].append(row)

    parts: list[str] = []
    attachments: list[Path] = []

    if buckets["submitted"]:
        parts.append(_render_submitted(buckets["submitted"]))
    if buckets["submitted_unrecorded"]:
        # xhigh-BLOCKING/H3
        parts.append(_render_submitted_unrecorded(buckets["submitted_unrecorded"]))
    if buckets["review_required"]:
        block, atts = _render_review_required(buckets["review_required"])
        parts.append(block)
        attachments.extend(atts)
    if buckets["auto_declined"]:
        parts.append(_render_auto_declined(buckets["auto_declined"]))
    if buckets["soft_dup"]:
        parts.append(_render_soft_dup(buckets["soft_dup"]))
    if buckets["bootstrap_needed"]:
        parts.append(_render_bootstrap_needed(buckets["bootstrap_needed"]))

    if not parts:
        return "", []

    # Dedup attachments by resolved absolute path (spec Acceptance #7).
    seen: set[Path] = set()
    deduped: list[Path] = []
    for att in attachments:
        try:
            key = att.resolve()
        except OSError:
            key = att.absolute()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(att)

    return "\n\n".join(parts), deduped


def _decision_status_to_bucket(status: Any) -> str | None:
    """H11: map a `Decision.status` string to the digest bucket kind."""
    if status == "submitted":
        return "submitted"
    if status == "submitted_unrecorded":
        # xhigh-BLOCKING/H3
        return "submitted_unrecorded"
    if status == "review_required":
        return "review_required"
    if status == "auto_declined":
        return "auto_declined"
    if status == "declined":
        # Explicit operator NO: fold into auto_declined bucket so it still
        # renders (dedicated bucket would require a new _BLOCK_ORDER entry).
        return "auto_declined"
    if status == "soft_dup_warn":
        return "soft_dup"
    return None


def _decision_to_row(dec: Any) -> dict:
    """H11: extract the digest row shape from a `Decision` dataclass.

    Preserves per-bucket rendering: `Submitted` reads `ats` + `application_id`;
    `Review required` reads `gmail_thread_id` + `screenshot_path`;
    `Auto-declined` reads `review_id`.

    xhigh-H6 (iter2): the `Decision` dataclass carries neither
    `application_id` nor `reason`, so both come back as None on this path.
    The `_render_submitted_unrecorded` bucket surfaces the review_id +
    the review's apply_url anchor so the operator can still triage without
    the underlying exception name. If callers need the exception name in
    the digest, they should attach the ApplyResult to the processed job
    (via `processed_apply_results`) which DOES carry `reason`.
    """
    return {
        "ats": getattr(dec, "ats", None),
        "application_id": getattr(dec, "application_id", None),
        "review_id": getattr(dec, "review_id", None),
        "gmail_thread_id": getattr(dec, "thread_id", None) or None,
        "company": getattr(dec, "company", None),
        "role_title": getattr(dec, "role_title", None),
        "apply_url": getattr(dec, "apply_url", None),
    }


def _read_status(ar: Any) -> Any:
    if isinstance(ar, dict):
        return ar.get("status")
    return getattr(ar, "status", None)


def _apply_result_to_row(ar: Any) -> dict:
    """Whitelist-copy fields off an ``ApplyResult`` into the row shape the
    per-block renderers expect. NEVER copy candidate PII — only structural
    identifiers + reason/status/ats.
    """
    if isinstance(ar, dict):
        def get(k: str, default: Any = None) -> Any:
            return ar.get(k, default)
    else:
        def get(k: str, default: Any = None) -> Any:
            return getattr(ar, k, default)

    return {
        "ats": get("ats"),
        "application_id": get("application_id"),
        "review_id": get("review_id"),
        "reason": get("reason"),
        # These may be attached by the seam when synthesizing from
        # ApplyResult; leave absent otherwise.
        "gmail_thread_id": get("gmail_thread_id"),
        "screenshot_path": _stringify_path(get("confirmation_screenshot")),
        "company": get("company"),
        "similar_role": get("similar_role"),
    }


def _stringify_path(v: Any) -> Any:
    if v is None:
        return None
    return str(v)


# ---------------------------------------------------------------------------
# Per-block renderers.
# ---------------------------------------------------------------------------


def _render_submitted(rows: list[dict]) -> str:
    lines = ["## Submitted"]
    for row in rows:
        # L2-class fix (Phase 1 xhigh, angles A + C): `dict.get(k, default)`
        # only fires the default when the key is MISSING — not when the
        # key is present with value None. Decision→row helpers explicitly
        # set application_id/ats to None for events without them, so the
        # digest previously rendered '- Submitted to None — application_id None'.
        ats = row.get("ats") or "unknown"
        app_id = row.get("application_id") or "unknown"
        lines.append(f"- Submitted to {ats} — application_id {app_id}")
    return "\n".join(lines)


def _render_submitted_unrecorded(rows: list[dict]) -> str:
    """xhigh-BLOCKING/H3 renderer. Surfaces DOM-verified submissions whose
    ``DedupDB.record()`` call failed — the ATS accepted the submission but
    the applied_jobs row never landed. Operators MUST see this because the
    next run's ``was_applied`` precheck will miss and the agent could
    silently double-apply if the DB glitch persists.

    xhigh-H6 (iter2): fall back to review_id / apply_url when application_id
    or reason are None (Decision-shape rows carry neither). Operators need
    at least one anchor to triage the affected row in the DB.
    """
    lines = ["## Submitted (not recorded — double-submit risk)"]
    for row in rows:
        ats = row.get("ats") or "unknown"
        app_id = row.get("application_id") or "unknown"
        reason = row.get("reason")
        review_id = row.get("review_id") or "<unknown>"
        apply_url = row.get("apply_url") or "<unknown-url>"
        # Compose a diagnostic line that ALWAYS carries at least one
        # locatable identifier (review_id) + the anchor (apply_url) even
        # when the ApplyResult layer (which carries reason) isn't in play.
        parts = [f"- Submitted_unrecorded to {ats}"]
        parts.append(f"application_id {app_id}")
        if reason:
            parts.append(f"(record failed: {reason})")
        parts.append(f"[review_id={review_id}, url={apply_url}]")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _render_review_required(rows: list[dict]) -> tuple[str, list[Path]]:
    lines = ["## Review required"]
    attachments: list[Path] = []
    for row in rows:
        # L2 fix: `_apply_result_to_row` and `_decision_to_row` unconditionally
        # set `gmail_thread_id` even when the underlying object doesn't carry
        # one — so ``dict.get("gmail_thread_id", default)`` returned the LITERAL
        # None (the default never fired). Explicit None-check + review_id
        # fallback so operators never see 'reply YES to None to submit'.
        thread_id = row.get("gmail_thread_id") or row.get("review_id") or "<unknown-thread>"
        lines.append(f"- reply YES to {thread_id} to submit")

        screenshot = row.get("screenshot_path")
        if not screenshot:
            continue
        path = Path(str(screenshot))
        if path.exists():
            attachments.append(path)
        else:
            # L7-safe log: identify by review_id, never leak the path (which
            # could belong to another user's workspace).
            # PR #12 iter-2 sweep — same L2 class: `.get()` returns None
            # when the key exists with value None (Decision-shape rows).
            _log.info(
                "digest.screenshot_missing review_id=%s",
                row.get("review_id") or "<unknown>",
            )
    return "\n".join(lines), attachments


def _render_auto_declined(rows: list[dict]) -> str:
    lines = ["## Auto-declined"]
    for row in rows:
        # L2-class fix — see _render_submitted comment. `or` fallback so a
        # None value from the Decision-shape row still renders '<unknown>'.
        review_id = row.get("review_id") or "<unknown>"
        lines.append(f"- Auto-declined — no reply in 72 h (review_id {review_id})")
    return "\n".join(lines)


def _render_soft_dup(rows: list[dict]) -> str:
    lines = ["## Blocked (soft-dup)"]
    for row in rows:
        # L2-class fix — see _render_submitted comment.
        company = row.get("company") or "<company>"
        review_id = row.get("review_id") or "<unknown>"
        lines.append(
            f"- Blocked (soft-dup) — similar role at {company}: "
            f"reply YES {review_id} to override"
        )
    return "\n".join(lines)


def _render_bootstrap_needed(rows: list[dict]) -> str:
    """Deduplicated by ATS per spec Acceptance #8.

    PR #12 iter-2 sweep: `_apply_result_to_row` at digest.py sets
    `"ats": get("ats")` — so `.get("ats", default)` returned literal
    None when the underlying object carried no `ats`. Same L2-class fix
    pattern applied throughout the other per-block renderers:
    `.get(...) or default` coerces both None and "" to the operator-
    facing placeholder.

    Dedup semantics: rows with unknown ATS (None or "") collapse into a
    SINGLE `<ats>` line — the operator's actionable signal is "at least
    one ATS session expired with no identifiable name; investigate."
    Two `<ats>`-placeholder lines would be identical bytes and carry no
    additional signal (iter-3 review: contrarian + pessimist + sweep
    consensus). The dedup key IS the render label so both stay in sync.
    """
    lines = ["## Bootstrap needed"]
    seen_ats: set[str] = set()
    for row in rows:
        ats = row.get("ats") or "<ats>"
        if ats in seen_ats:
            continue
        seen_ats.add(ats)
        lines.append(f"- Bootstrap needed — {ats} session expired")
    return "\n".join(lines)
