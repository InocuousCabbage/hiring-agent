"""
tests/test_hm_finder.py — Tests for hiring manager lookup feature.

Unit tests for:
  - JD clue extraction regex
  - Seniority inference logic
  - Response parsing (mock API responses)
  - Config-disabled path
  - Digest formatting with/without HM info

Integration test (live API) gated by HIRING_AGENT_LIVE_TEST env var.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from contacts.hm_finder import (
    extract_jd_clues,
    infer_hm_seniority,
    parse_hm_response,
    find_hiring_manager,
)


# ═══════════════════════════════════════════════════════════════════════════════
# JD Clue Extraction
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractJdClues:
    def test_reports_to_basic(self):
        jd = "This role reports to the VP of Marketing."
        clues = extract_jd_clues(jd)
        assert clues["reports_to"] is not None
        assert "VP of Marketing" in clues["reports_to"]

    def test_reporting_to_variant(self):
        jd = "The analyst will be reporting to the Director of Product Marketing."
        clues = extract_jd_clues(jd)
        assert clues["reports_to"] is not None
        assert "Director" in clues["reports_to"]

    def test_reports_directly_to(self):
        jd = "You will report directly to the CMO."
        clues = extract_jd_clues(jd)
        assert clues["reports_to"] is not None
        assert "CMO" in clues["reports_to"]

    def test_no_clues(self):
        jd = "We are looking for a marketing manager to join our team."
        clues = extract_jd_clues(jd)
        assert clues["reports_to"] is None
        assert clues["hm_name"] is None

    def test_hiring_manager_name(self):
        jd = "Hiring Manager: Jane Smith\nApply below."
        clues = extract_jd_clues(jd)
        assert clues["hm_name"] == "Jane Smith"

    def test_contact_name(self):
        jd = "Contact: John Doe for more info."
        clues = extract_jd_clues(jd)
        assert clues["hm_name"] == "John Doe"


# ═══════════════════════════════════════════════════════════════════════════════
# Seniority Inference
# ═══════════════════════════════════════════════════════════════════════════════


class TestInferHmSeniority:
    def test_coordinator_maps_to_manager(self):
        titles = infer_hm_seniority("Marketing Coordinator")
        assert "manager" in titles

    def test_manager_maps_to_director(self):
        titles = infer_hm_seniority("Product Marketing Manager")
        assert "director" in titles

    def test_director_maps_to_vp(self):
        titles = infer_hm_seniority("Director of Content")
        assert "vp" in titles

    def test_vp_maps_to_chief(self):
        titles = infer_hm_seniority("VP of Marketing")
        assert "chief" in titles or "cmo" in titles

    def test_intern_maps_to_manager(self):
        titles = infer_hm_seniority("Marketing Intern")
        assert "manager" in titles

    def test_unknown_defaults_to_director(self):
        titles = infer_hm_seniority("Growth Hacker")
        assert "director" in titles

    def test_senior_maps_to_manager(self):
        titles = infer_hm_seniority("Senior Content Strategist")
        assert "manager" in titles

    def test_senior_manager_maps_to_director(self):
        titles = infer_hm_seniority("Senior Manager of Content")
        assert "director" in titles
        # Should NOT match plain "manager" entry (which includes "senior director")
        assert "senior director" in titles

    def test_head_of_maps_to_vp(self):
        titles = infer_hm_seniority("Head of Product Marketing")
        assert "vp" in titles


# ═══════════════════════════════════════════════════════════════════════════════
# Response Parsing
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseHmResponse:
    """parse_hm_response now accepts a plain text string (CLI output)."""

    def test_valid_json(self):
        raw = json.dumps({
            "name": "Sarah Chen",
            "title": "VP of Marketing",
            "linkedin_url": "https://linkedin.com/in/sarachen",
            "email": "sarah@acme.com",
            "confidence": "high",
            "outreach_note": "Hi Sarah, saw the PMM role at Acme.",
        })
        result = parse_hm_response(raw)
        assert result is not None
        assert result["name"] == "Sarah Chen"
        assert result["title"] == "VP of Marketing"
        assert result["confidence"] == "high"

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"name": "Jane Doe", "title": "Director"}\n```'
        result = parse_hm_response(raw)
        assert result is not None
        assert result["name"] == "Jane Doe"

    def test_json_with_surrounding_text(self):
        raw = (
            'Based on my search, here is the result:\n'
            '{"name": "Bob Smith", "title": "CMO", "confidence": "medium"}\n'
            'Hope that helps!'
        )
        result = parse_hm_response(raw)
        assert result is not None
        assert result["name"] == "Bob Smith"

    def test_null_name_returns_none(self):
        raw = '{"name": null, "title": null, "confidence": "low"}'
        result = parse_hm_response(raw)
        assert result is None

    def test_empty_name_returns_none(self):
        raw = '{"name": "", "title": "Director"}'
        result = parse_hm_response(raw)
        assert result is None

    def test_invalid_json_returns_none(self):
        raw = "I couldn't find any hiring manager information."
        result = parse_hm_response(raw)
        assert result is None

    def test_empty_response_returns_none(self):
        result = parse_hm_response("")
        assert result is None

    def test_none_response_returns_none(self):
        result = parse_hm_response(None)
        assert result is None

    def test_missing_optional_fields(self):
        raw = '{"name": "Test Person", "confidence": "low"}'
        result = parse_hm_response(raw)
        assert result is not None
        assert result["name"] == "Test Person"
        assert result["title"] is None
        assert result["linkedin_url"] is None

    def test_invalid_confidence_defaults_to_low(self):
        raw = json.dumps({
            "name": "Test Person",
            "confidence": "medium-high",
        })
        result = parse_hm_response(raw)
        assert result is not None
        assert result["confidence"] == "low"


# ═══════════════════════════════════════════════════════════════════════════════
# Config-disabled path
# ═══════════════════════════════════════════════════════════════════════════════


class TestFindHiringManagerDisabled:
    def test_returns_none_when_disabled(self):
        result = find_hiring_manager(
            job={"title": "PM", "company": "Acme", "url": "https://example.com"},
            jd_text="Some job description.",
            lane="Product Marketing (PMM)",
            config={"enabled": False},
        )
        assert result is None

    def test_returns_none_when_enabled_missing(self):
        """Default for enabled should be False (safe default)."""
        result = find_hiring_manager(
            job={"title": "PM", "company": "Acme", "url": "https://example.com"},
            jd_text="Some job description.",
            lane="Product Marketing (PMM)",
            config={},
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# find_hiring_manager with mocked API
# ═══════════════════════════════════════════════════════════════════════════════


class TestFindHiringManagerMocked:
    def test_successful_lookup(self):
        mock_response = json.dumps({
            "name": "Sarah Chen",
            "title": "VP of Marketing",
            "linkedin_url": "https://linkedin.com/in/sarachen",
            "email": None,
            "confidence": "high",
            "outreach_note": "Hi Sarah, excited about the PMM role.",
        })

        with patch("contacts.hm_finder.call_claude", return_value=mock_response):
            result = find_hiring_manager(
                job={"title": "Product Marketing Manager", "company": "Acme",
                     "url": "https://example.com", "location": "Remote"},
                jd_text="We need a PMM. Reports to the VP of Marketing.",
                lane="Product Marketing (PMM)",
                config={"enabled": True, "model": "claude-haiku-4-5-20251001",
                        "timeout_seconds": 30,
                        "generate_outreach_note": True, "outreach_note_max_words": 60},
            )

        assert result is not None
        assert result["name"] == "Sarah Chen"

    def test_cli_error_returns_none(self):
        with patch("contacts.hm_finder.call_claude", side_effect=Exception("CLI timeout")):
            result = find_hiring_manager(
                job={"title": "PM", "company": "Acme", "url": "https://example.com"},
                jd_text="Some JD",
                lane="PMM",
                config={"enabled": True},
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Digest formatting with HM info
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestWithHiringManager:
    def test_digest_includes_hm_info(self):
        from gmail.digest import compose_digest

        processed = [{
            "title": "PMM",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
            "hiring_manager": {
                "name": "Sarah Chen",
                "title": "VP of Marketing",
                "linkedin_url": "https://linkedin.com/in/sarachen",
                "email": "sarah@acme.com",
                "confidence": "high",
                "outreach_note": "Hi Sarah, saw the PMM opening.",
            },
        }]
        body = compose_digest(processed=processed, skipped=[])
        assert "Sarah Chen" in body
        assert "VP of Marketing" in body
        assert "linkedin.com/in/sarachen" in body
        assert "sarah@acme.com" in body
        assert "Hi Sarah" in body

    def test_digest_without_hm_info(self):
        from gmail.digest import compose_digest

        processed = [{
            "title": "PMM",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
        }]
        body = compose_digest(processed=processed, skipped=[])
        assert "PMM" in body
        assert "Hiring Manager" not in body

    def test_digest_with_partial_hm_info(self):
        from gmail.digest import compose_digest

        processed = [{
            "title": "PMM",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
            "hiring_manager": {
                "name": "Jane Doe",
                "title": "Director",
                "linkedin_url": None,
                "email": None,
                "confidence": "low",
                "outreach_note": None,
            },
        }]
        body = compose_digest(processed=processed, skipped=[])
        assert "Jane Doe" in body
        assert "Director" in body
        # Should not have LinkedIn/Email/Outreach lines when null
        assert "LinkedIn:" not in body
        assert "Email:" not in body
        assert "Outreach:" not in body

    def test_digest_with_null_hm(self):
        from gmail.digest import compose_digest

        processed = [{
            "title": "PMM",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
            "hiring_manager": None,
        }]
        body = compose_digest(processed=processed, skipped=[])
        assert "PMM" in body
        assert "Hiring Manager" not in body


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test (live API, gated)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not os.getenv("HIRING_AGENT_LIVE_TEST"),
    reason="Set HIRING_AGENT_LIVE_TEST=1 to run live API tests",
)
class TestHmFinderLive:
    def test_live_lookup(self):
        result = find_hiring_manager(
            job={
                "title": "Product Marketing Manager",
                "company": "Anthropic",
                "url": "https://example.com",
                "location": "San Francisco, CA",
            },
            jd_text=(
                "We are looking for a Product Marketing Manager to join our team. "
                "This role reports to the Head of Product Marketing. "
                "Responsibilities include go-to-market strategy, competitive analysis, "
                "and sales enablement."
            ),
            lane="Product Marketing (PMM)",
            config={
                "enabled": True,
                "model": "claude-haiku-4-5-20251001",
                "max_web_searches": 3,
                "timeout_seconds": 30,
                "generate_outreach_note": True,
                "outreach_note_max_words": 60,
            },
        )
        # May or may not find someone, but should not crash
        if result:
            assert result["name"]
            assert result["confidence"] in ("high", "medium", "low")
            print(f"\nFound: {result['name']} — {result['title']}")
            if result["outreach_note"]:
                print(f"Outreach: {result['outreach_note']}")
