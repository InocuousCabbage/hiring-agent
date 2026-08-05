"""Tests for src/utils/llm_json.py — robuster JSON extraction from LLM output.

Real failures logged in pipeline.log (raw REDACTED so shapes are synthesised
from the error messages, not the exact bytes):

  2026-08-03 09:41:24 Onbe        "Expecting value: line 1 column 1 (char 0)"
  2026-08-03 09:44:08 Medtronic   "Expecting value: line 1 column 1 (char 0)"
  2026-08-05 09:42:09 Cutover     "Extra data: line 56 column 1 (char 2790)"

char-0 = leading garbage before the JSON begins (prose preamble or wrong content)
extra-data = valid JSON followed by more content (trailing prose, second obj)

Genuinely-broken (unclosed brace, invalid syntax) must NOT be silently rescued —
the whole point of the fix is to distinguish parse-error from genuinely-poor-match,
not to collapse "unrecoverably broken" into "recovered".
"""
from __future__ import annotations

import pytest

from src.utils.llm_json import (
    LLMJsonParseError,
    extract_json_object,
)


# ── Happy path ─────────────────────────────────────────────────────────────

def test_pure_json_object_parses_cleanly():
    """No wrapping content, plain JSON. Baseline: must not regress."""
    raw = '{"confidence_score": 87, "tagline": "Test"}'
    result = extract_json_object(raw)
    assert result == {"confidence_score": 87, "tagline": "Test"}


def test_json_with_markdown_fences_parses():
    """The pre-fix code already stripped fences. Preserve that behaviour."""
    raw = '```json\n{"confidence_score": 87}\n```'
    result = extract_json_object(raw)
    assert result == {"confidence_score": 87}


# ── Onbe/Medtronic shape: char-0 error, leading prose ─────────────────────

def test_prose_preamble_before_json_is_extracted():
    """The model prefaced its JSON with an explanation paragraph.
    Real error: 'Expecting value: line 1 column 1 (char 0)'"""
    raw = (
        "Here is the tailored resume based on the candidate's background:\n\n"
        '{"confidence_score": 72, "tagline": "Data-driven marketer"}\n'
    )
    result = extract_json_object(raw)
    assert result["confidence_score"] == 72


def test_multi_line_prose_preamble_before_json():
    """A more verbose preamble — several paragraphs before the JSON block."""
    raw = (
        "I've reviewed the resume and job posting.\n\n"
        "The candidate has a moderate match for this role. Key strengths:\n"
        "- Marketing automation experience\n"
        "- CRM familiarity\n\n"
        "Here is the tailored resume:\n"
        '{"confidence_score": 65, "tagline": "Marketing ops specialist"}'
    )
    result = extract_json_object(raw)
    assert result["confidence_score"] == 65


# ── Cutover shape: extra-data error, trailing prose ──────────────────────

def test_trailing_prose_after_json_is_ignored():
    """The model closed its JSON then added an explanation.
    Real error: 'Extra data: line 56 column 1 (char 2790)'"""
    raw = (
        '{"confidence_score": 55, "tagline": "Adaptable"}\n\n'
        "Note: I've flagged some resume gaps in the response above.\n"
        "Please review before submission."
    )
    result = extract_json_object(raw)
    assert result["confidence_score"] == 55


def test_trailing_second_json_object_ignored():
    """Model returned two JSON objects; we take the first + ignore the second."""
    raw = '{"confidence_score": 40}\n{"debug": "info"}'
    result = extract_json_object(raw)
    assert result["confidence_score"] == 40


# ── Prose both sides ─────────────────────────────────────────────────────

def test_prose_both_before_and_after_json():
    raw = (
        "Here is the resume:\n"
        '{"confidence_score": 82}\n'
        "Let me know if you need adjustments."
    )
    result = extract_json_object(raw)
    assert result["confidence_score"] == 82


# ── Genuinely broken: must raise, not silently recover ───────────────────

def test_unclosed_brace_raises_distinct_error():
    """The bug this fix exists to prevent recreating: silent-0 fallback
    collapses parser-broke and genuine-poor-match. Genuinely-broken
    output must raise an LLMJsonParseError so the caller can distinguish
    'the parser could not extract' from 'the model returned confidence 0'."""
    raw = 'Here is the resume: {"confidence_score": 50, "tagline": "unfinished'
    with pytest.raises(LLMJsonParseError):
        extract_json_object(raw)


def test_no_json_at_all_raises():
    """Model returned pure prose with no JSON. Must raise, not silently
    return an empty dict (silent-empty is what the pre-fix silent-0 did)."""
    raw = (
        "I cannot generate a resume for this candidate — their background "
        "does not match the role requirements at all."
    )
    with pytest.raises(LLMJsonParseError):
        extract_json_object(raw)


def test_empty_string_raises():
    with pytest.raises(LLMJsonParseError):
        extract_json_object("")


def test_only_opening_brace_raises():
    with pytest.raises(LLMJsonParseError):
        extract_json_object("{")


# ── Sentinel discipline: error carries diagnostic info ───────────────────

def test_parse_error_carries_raw_prefix_for_diagnosis():
    """When the extractor gives up, the raised error must include enough
    of the raw content that the operator can see what went wrong.
    Redacted raw in logs is why we can't pull real fixtures for this fix;
    the error message needs to be diagnostic even when raw is redacted."""
    raw = "no json anywhere here"
    with pytest.raises(LLMJsonParseError) as exc_info:
        extract_json_object(raw)
    # The error should give SOMETHING actionable — length, first-chars, or a
    # named reason — not just "parse failed."
    msg = str(exc_info.value)
    assert len(msg) > 20, f"error message too terse to diagnose: {msg!r}"


def test_nested_json_still_parses_correctly():
    """Balanced-brace walk must count nested braces, not stop at first }."""
    raw = '{"outer": {"inner": {"deep": 1}}, "sibling": true}'
    result = extract_json_object(raw)
    assert result["outer"]["inner"]["deep"] == 1
    assert result["sibling"] is True


def test_json_with_string_containing_braces():
    """A string value containing { or } must not confuse the brace-count.
    'name': 'Acme {Corp}' has braces inside a string that don't count."""
    raw = '{"name": "Acme {Corp}", "confidence_score": 90}'
    result = extract_json_object(raw)
    assert result["name"] == "Acme {Corp}"
    assert result["confidence_score"] == 90
