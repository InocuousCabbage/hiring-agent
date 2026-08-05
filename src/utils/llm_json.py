"""Robuster JSON extraction from LLM output.

The Anthropic Claude API sometimes returns JSON with prose padding around it:
a preamble explaining what the model is about to produce, or trailing commentary
after the JSON block closes. `json.loads` fails in both cases with distinctive
error messages (`Expecting value: line 1 column 1 (char 0)` for prose-preamble,
`Extra data: line 56 column 1 (char 2790)` for trailing-content), which the
pre-fix code caught as `json.JSONDecodeError` and mapped to a silent
`confidence_score = 0` fallback.

That silent-0 mapped BOTH parser-broke and genuine-poor-match into the same
signal, which is the load-bearing bug this module exists to fix. Recovery
happens in two directions:

  1. **Extraction:** find the first balanced JSON object inside the raw
     content by walking brace-depth (respecting string literals so `{` and
     `}` inside a string don't confuse the count).
  2. **Distinction:** if extraction fails, raise `LLMJsonParseError` with
     a diagnostic message. Callers can now distinguish "parser gave up on
     this response" from "the model returned confidence 0" — different
     recovery paths.

Genuinely broken responses (unclosed braces, non-JSON prose only, empty
output) MUST raise rather than silently recover. Silent-recovery is what
made the pre-fix behaviour dangerous; extraction-recovery for the two
known-bad shapes is a different move.
"""
from __future__ import annotations

import json
import re
from typing import Any


class LLMJsonParseError(RuntimeError):
    """Raised when we cannot extract a JSON object from LLM output.

    Distinct from `json.JSONDecodeError` so callers can tell "the JSON
    extractor gave up" from "raw json.loads found bad JSON." Also distinct
    from a legitimately-parsed result with confidence_score=0, which is a
    valid model output signalling poor match.

    The message contains diagnostic detail (why extraction failed + a
    prefix of the raw content) so an operator seeing this in logs can
    figure out what shape the model returned without needing the raw
    saved separately.
    """


# ── Public API ────────────────────────────────────────────────────────────


def extract_json_object(raw: str) -> dict[str, Any]:
    """Return the first balanced JSON object embedded in `raw`.

    Tolerates:
      - Leading markdown code fences (```json ... ```)
      - Trailing markdown code fences
      - Prose preamble before the JSON (Anthropic occasional shape)
      - Prose or additional content after the JSON closes
      - Nested objects (brace counter respects nesting)
      - String literals containing `{` or `}` (parser respects string state)

    Raises:
      LLMJsonParseError: if no balanced JSON object can be extracted, OR
        if extraction produced content that isn't a dict at the top level.
    """
    stripped = _strip_markdown_fences(raw)
    if not stripped:
        raise LLMJsonParseError(
            f"empty content after markdown-fence strip (raw len={len(raw)})"
        )

    # Fast path: the whole thing is clean JSON. Try it first; most calls
    # hit this branch, and skipping the walk keeps the happy path cheap.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return parsed

    # Slow path: find the first balanced object. Walks the string once,
    # counting brace depth while respecting string-literal state.
    obj_start, obj_end = _first_balanced_object_span(stripped)
    if obj_start is None or obj_end is None:
        raise LLMJsonParseError(
            f"no balanced JSON object found in raw content "
            f"(len={len(raw)}, prefix={raw[:80]!r})"
        )

    candidate = stripped[obj_start:obj_end]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMJsonParseError(
            f"balanced-brace region did not parse as JSON: {exc.msg} "
            f"(region len={len(candidate)}, prefix={candidate[:80]!r})"
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMJsonParseError(
            f"extracted JSON is not an object (got {type(parsed).__name__}); "
            f"the pipeline expects an object at the top level"
        )
    return parsed


# ── Internals ─────────────────────────────────────────────────────────────


_FENCE_LEAD = re.compile(r"^```(?:json)?\s*", flags=re.MULTILINE)
_FENCE_TRAIL = re.compile(r"\s*```\s*$", flags=re.MULTILINE)


def _strip_markdown_fences(raw: str) -> str:
    """Strip leading and trailing ```json / ``` fences.

    Deliberately conservative: only touches fences at the very start and
    end of the content. Fences elsewhere (in the middle of the response)
    are left alone so the brace-walk can still find the JSON.
    """
    s = raw.strip()
    s = _FENCE_LEAD.sub("", s, count=1)
    s = _FENCE_TRAIL.sub("", s, count=1)
    return s.strip()


def _first_balanced_object_span(s: str) -> tuple[int | None, int | None]:
    """Return (start, end) indices of the first balanced `{...}` in `s`.

    Walks the string once, entering "string mode" on unescaped `"` so that
    braces inside string literals don't count. Returns (None, None) if
    no balanced object exists.

    Only recognises `{...}` object spans. A top-level array `[...]` is
    intentionally not supported: the resume/cover pipeline expects an
    object at the top level, and quietly accepting an array would push
    the type failure downstream where the recovery is worse.
    """
    depth = 0
    start: int | None = None
    in_string = False
    escape_next = False

    for i, ch in enumerate(s):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                # Unmatched close before any open — skip; keep scanning
                # in case a valid object appears later.
                continue
            depth -= 1
            if depth == 0 and start is not None:
                return start, i + 1

    # Reached end of string without closing all opens — unbalanced.
    return None, None
