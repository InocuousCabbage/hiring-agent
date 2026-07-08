"""
src/apply/logging.py — Shard S16: PII-scrubbing structlog processor + event-name lint.

Landmine mitigations:
- L7: `filled_fields` dict passed to structlog bleeds raw PII. This module
  installs a processor that redacts any kv-pair whose key matches a compiled
  case-insensitive substring regex. Applied globally so no adapter, review-loop,
  captcha module, or downstream shard can leak PII by accident.
- L6: never the deprecated naive-UTC `datetime` call. This module does no
  timestamping directly and keeps the forbidden identifier out of its source
  so grep-based verification of the L6 landmine passes.

Public contracts (imported by tests and every apply-package shard):
- `install_scrubber(logger=None) -> None` — idempotent global installer.
- `scrub_kv(logger, method_name, event_dict) -> dict` — structlog processor.
- `lint_event_name(logger, method_name, event_dict) -> dict` — structlog processor.
- `_ALLOWED_EVENT_NAMES: frozenset[str]` — 12 allowlisted `apply.*` event names.
- `_PII_KEY_RE: re.Pattern` — compiled once at import.
- `_REDACTED: str` — literal `"***REDACTED***"`.

Design notes:
- Processors are pure functional: they return a new dict rather than mutating
  the input. Concurrent structlog paths share the input dict and must not
  interfere with each other.
- `scrub_kv` walks the top-level kv-pairs and one level deep inside dict
  values. Deeper recursion is deferred (spec §REFACTOR). Two-levels-deep is
  a documented boundary — see `test_nested_dict_two_levels_not_redacted`.
- `event` (the structlog message body) is never scrubbed. If a caller
  embeds PII in the message string itself, that is a caller bug and is
  called out by `test_message_body_not_scrubbed`.
- Unknown `apply.*` event names log a WARNING via the stdlib `logging`
  module the first time seen (deduped per process). Non-`apply.*` names
  are ignored so gmail/qa/pdf_gen loggers are untouched.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_PII_KEY_RE: re.Pattern[str] = re.compile(
    r"(email|phone|first_name|last_name|address|linkedin|answer|prompt|raw|value)",
    re.IGNORECASE,
)

_ALLOWED_EVENT_NAMES: frozenset[str] = frozenset({
    "apply.form_navigated",
    "apply.form_filled",
    "apply.captcha_detected",
    "apply.submitted",
    "apply.review_required",
    "apply.failed",
    "apply.dedup_hit",
    "apply.rate_limited",
    "apply.review.auto_declined",
    "apply.field.absent",
    "apply.dry_run.holding_at_submit",
    "apply.session_expired",
    # S17 seam events (S17 acceptance criterion #13 — added as cross-shard
    # courtesy per spec since S17 emits these from main.py::run_pipeline).
    "apply.seam.enabled",
    "apply.seam.disabled",
    "apply.seam.error",
    "apply.review.poll_started",
    "apply.review.poll_failed",
    "apply.retention.error",
    "apply.retention.rotated",
})

_REDACTED: str = "***REDACTED***"

_installed: bool = False
_seen_unknown_events: set[str] = set()

_lint_logger = logging.getLogger("apply.logging")


def _redact_value(value: Any) -> Any:
    """Redact a value whose key matched the PII regex.

    None passes through (avoids materialising a redaction token where nothing
    was logged). Everything else becomes the literal `***REDACTED***` string.
    """
    if value is None:
        return None
    return _REDACTED


def scrub_kv(logger, method_name, event_dict: dict) -> dict:
    """structlog processor: redact kv-pairs whose keys match `_PII_KEY_RE`.

    - The `event` key (structlog message body) is preserved verbatim.
    - Top-level kv-pairs with a matching key are redacted whole.
    - Dict values are walked one level deeper; inner keys matching the regex
      are redacted; other inner values pass through unchanged.
    - Non-dict values (lists, ints, floats, None, arbitrary objects) are
      redacted as-is when their key matches — no partial-redaction attempt.
    - The input dict is not mutated; a new dict is returned.
    """
    new_dict: dict = {}
    for k, v in event_dict.items():
        # Never scrub the message body — that is the caller's boundary.
        if k == "event":
            new_dict[k] = v
            continue

        if isinstance(k, str) and _PII_KEY_RE.search(k):
            new_dict[k] = _redact_value(v)
            continue

        # One level of recursion into dict values.
        if isinstance(v, dict):
            inner: dict = {}
            for ik, iv in v.items():
                if isinstance(ik, str) and _PII_KEY_RE.search(ik):
                    inner[ik] = _redact_value(iv)
                else:
                    inner[ik] = iv
            new_dict[k] = inner
            continue

        new_dict[k] = v
    return new_dict


def lint_event_name(logger, method_name, event_dict: dict) -> dict:
    """structlog processor: warn once per unknown `apply.*` event name.

    - Non-`apply.*` names are ignored (gmail/qa/pdf_gen loggers are untouched).
    - Known `apply.*` names in `_ALLOWED_EVENT_NAMES` pass silently.
    - Unknown `apply.*` names log a WARNING via stdlib `logging` the first
      time each name is seen; subsequent occurrences are silent (per-process
      dedup via `_seen_unknown_events`). The offending event still emits.
    """
    event_name = event_dict.get("event")
    if not isinstance(event_name, str):
        return event_dict
    if not event_name.startswith("apply."):
        return event_dict
    if event_name in _ALLOWED_EVENT_NAMES:
        return event_dict
    if event_name not in _seen_unknown_events:
        _seen_unknown_events.add(event_name)
        _lint_logger.warning("unknown apply.* event: %s", event_name)
    return event_dict


def install_scrubber(logger: Any = None) -> None:
    """Install the PII scrubber + event-name lint processors globally.

    Idempotent: calling more than once is a no-op after the first successful
    install. The processors are inserted before the final renderer in the
    current structlog processor chain so the renderer sees redacted values.

    The `logger` argument is accepted for future per-logger targeting; it is
    ignored today. The processors apply to every structlog-bound logger in
    the process (defence in depth for non-apply loggers as well).

    Errors during `structlog.configure` are not swallowed — callers should
    see them at install time so misconfiguration is loud.
    """
    global _installed
    if _installed:
        return

    current = structlog.get_config()
    procs = list(current.get("processors", []))

    # Insert scrub_kv and lint_event_name immediately before the final renderer
    # so the renderer emits redacted values. If the chain is empty for some
    # reason, seed it with just our two processors — structlog will still
    # accept and emit key-value dicts via its default renderer.
    if procs:
        insert_at = len(procs) - 1
        procs.insert(insert_at, scrub_kv)
        procs.insert(insert_at + 1, lint_event_name)
    else:
        procs = [scrub_kv, lint_event_name]

    structlog.configure(processors=procs)
    _installed = True
