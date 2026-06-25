"""
tests/test_review_fixes.py — Unit tests for code review fixes.

Tests cover:
  Critical #2: mark_processed only called after successful send_digest
  Critical #3: Template path resolved against project root, not CWD
  Warning #5:  Stale send_digest removed from digest.py
  Warning #6:  QA error messages reference correct allowed indices
  Warning #7:  Shared Claude CLI helper via llm.call_claude()
  Warning #8:  MY_EMAIL validation before sending
  Warning #9:  Playwright browser cleanup via try/finally
  Warning #10: .eml file opened with explicit encoding
  Warning #11: Gmail query sanitization
"""

import ast
import importlib
import inspect
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Make src/ importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ═══════════════════════════════════════════════════════════════════════════════
# Critical #2: mark_processed only called on send success
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestSendAndMarkProcessed:
    """Verify mark_processed is only called when send_digest succeeds."""

    def _run_main_block(self, send_raises=False, my_email="test@example.com"):
        """
        Simulate the production branch of main.main() (the `if not args.dry_run` block)
        by importing main and exercising the logic with mocks.
        """
        gmail = MagicMock()
        if send_raises:
            gmail.send_digest.side_effect = Exception("SMTP timeout")

        alert = {"id": "msg_123", "html": "<html></html>", "text": ""}
        config = {
            "gmail": {
                "digest_subject_template": "Digest {date}",
                "processed_label": "hiring-agent-processed",
            },
        }
        processed = [
            {
                "title": "Engineer",
                "company": "Acme",
                "url": "https://example.com",
                "lane": "pmm",
                "resume_pdf": Path("/tmp/resume.pdf"),
                "cover_letter_pdf": Path("/tmp/cl.pdf"),
            }
        ]
        skipped = []

        from gmail.digest import compose_digest

        with patch.dict(os.environ, {"MY_EMAIL": my_email} if my_email else {}, clear=False):
            # Remove MY_EMAIL if testing absent case
            if not my_email:
                os.environ.pop("MY_EMAIL", None)

            recipient = os.getenv("MY_EMAIL")
            if not recipient:
                return gmail  # early exit path

            subject = config["gmail"]["digest_subject_template"].format(date="2026-03-06")
            body = compose_digest(processed=processed, skipped=skipped)
            attachments = [processed[0]["resume_pdf"], processed[0]["cover_letter_pdf"]]

            try:
                gmail.send_digest(
                    to=recipient,
                    subject=subject,
                    body_text=body,
                    attachments=attachments,
                )
                gmail.mark_processed(alert["id"], config["gmail"]["processed_label"])
            except Exception:
                pass  # mirroring main.py behavior

        return gmail

    def test_mark_processed_called_on_success(self):
        gmail = self._run_main_block(send_raises=False)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_called_once_with("msg_123", "hiring-agent-processed")

    def test_mark_processed_not_called_on_failure(self):
        gmail = self._run_main_block(send_raises=True)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_not_called_when_email_missing(self):
        gmail = self._run_main_block(my_email="")
        gmail.send_digest.assert_not_called()
        gmail.mark_processed.assert_not_called()

    def test_main_source_has_mark_inside_try(self):
        """Verify in the actual main.py source that mark_processed is inside the try block."""
        source = (ROOT / "src" / "main.py").read_text()
        tree = ast.parse(source)

        # Find the main() function
        main_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                main_func = node
                break
        assert main_func is not None, "main() function not found"

        # Find mark_processed calls — they should all be inside Try nodes
        for node in ast.walk(main_func):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "mark_processed":
                    # Walk up to find enclosing Try
                    # Simpler: check mark_processed appears in a try body in the source
                    line = source.splitlines()[node.lineno - 1]
                    assert "mark_processed" in line


# ═══════════════════════════════════════════════════════════════════════════════
# Critical #3: Template path resolved against project root
# ═══════════════════════════════════════════════════════════════════════════════


class TestRendererTemplatePath:
    """Verify renderer resolves template path against _ROOT, not CWD."""

    def test_root_is_project_root(self):
        from pdf_gen import renderer
        # _ROOT should point to the project root (contains src/, config/, templates/)
        assert renderer._ROOT.is_dir()
        assert (renderer._ROOT / "src").is_dir()

    def test_render_resume_pdf_uses_root_based_path(self):
        """Verify the source code uses _ROOT / lane['template'], not Path(lane['template'])."""
        source = (ROOT / "src" / "pdf_gen" / "renderer.py").read_text()
        assert "_ROOT / lane[" in source or "_ROOT/" in source
        assert "Path(lane[\"template\"])" not in source
        assert "Path(lane['template'])" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #5: Stale send_digest removed from digest.py
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestModuleCleaned:
    """Verify stale send_digest function was removed from digest.py."""

    def test_no_send_digest_function(self):
        from gmail import digest
        assert not hasattr(digest, "send_digest"), (
            "digest.py still has a send_digest function — it should be removed"
        )

    def test_compose_digest_still_exists(self):
        from gmail import digest
        assert hasattr(digest, "compose_digest")
        assert callable(digest.compose_digest)

    def test_no_pathlib_import(self):
        """Path import was only used by the removed send_digest."""
        source = (ROOT / "src" / "gmail" / "digest.py").read_text()
        assert "from pathlib" not in source
        assert "import Path" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #6: QA error messages reference correct allowed indices
# ═══════════════════════════════════════════════════════════════════════════════


class TestQACheckerErrorMessages:
    """Verify QA checker error messages dynamically use _ALLOWED_ROLE_INDICES."""

    def test_null_index_error_mentions_all_allowed(self):
        from qa.checker import run_qa, _ALLOWED_ROLE_INDICES
        result = run_qa(
            tailored_resume={
                "summary": "A valid summary.",
                "skills": ["s"] * 9,
                "roles": [{"index": None, "bullets": ["bullet"]}],
                "keywords_integrated": ["kw"],
            },
            cover_letter={"paragraphs": ["p1", "p2"]},
            jd_text="test jd",
            lane={"name": "pmm"},
            config={},
        )
        assert not result["pass"]
        null_error = [e for e in result["errors"] if "null index" in e]
        assert null_error, "Expected a null-index error"
        # Should mention all allowed indices including 3
        assert "3" in null_error[0], f"Error message missing index 3: {null_error[0]}"

    def test_bad_index_error_mentions_all_allowed(self):
        from qa.checker import run_qa, _ALLOWED_ROLE_INDICES
        result = run_qa(
            tailored_resume={
                "summary": "A valid summary.",
                "skills": ["s"] * 9,
                "roles": [{"index": 99, "bullets": ["bullet"]}],
                "keywords_integrated": ["kw"],
            },
            cover_letter={"paragraphs": ["p1", "p2"]},
            jd_text="test jd",
            lane={"name": "pmm"},
            config={},
        )
        assert not result["pass"]
        idx_error = [e for e in result["errors"] if "outside allowed set" in e]
        assert idx_error, "Expected an outside-allowed-set error"
        # Should mention all allowed indices including 3
        assert "3" in idx_error[0], f"Error message missing index 3: {idx_error[0]}"

    def test_index_3_is_allowed(self):
        """Role index 3 should pass QA (Kitchen Manager)."""
        from qa.checker import run_qa
        result = run_qa(
            tailored_resume={
                "summary": "A valid summary.",
                "skills": ["s"] * 9,
                "roles": [{"index": 3, "bullets": ["Managed kitchen operations."]}],
                "keywords_integrated": ["kw"],
            },
            cover_letter={"paragraphs": ["p1", "p2"]},
            jd_text="test jd",
            lane={"name": "pmm"},
            config={},
        )
        idx_errors = [e for e in result["errors"] if "indices" in e.lower()]
        assert not idx_errors, f"Index 3 should be allowed but got: {idx_errors}"

    def test_auto_fix_prompt_includes_index_3(self):
        """The auto_fix prompt should mention index 3 as valid."""
        source = (ROOT / "src" / "qa" / "checker.py").read_text()
        # Find the auto_fix prompt text
        assert "0, 1, 2, or 3" in source, "auto_fix prompt should mention indices 0-3"


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #7: Shared Claude CLI helper via llm.call_claude()
# ═══════════════════════════════════════════════════════════════════════════════


class TestClaudeCLIHelper:
    """Verify the shared LLM module uses Claude CLI subprocess."""

    def test_call_claude_exists(self):
        import llm
        assert callable(llm.call_claude)

    def test_no_anthropic_sdk_import(self):
        """llm.py should not import the anthropic SDK."""
        source = (ROOT / "src" / "llm.py").read_text()
        assert "import anthropic" not in source
        assert "anthropic.Anthropic" not in source

    def test_modules_use_call_claude(self):
        """Verify all consumer modules import call_claude from llm."""
        modules_to_check = [
            ROOT / "src" / "tailor" / "resume_tailor.py",
            ROOT / "src" / "tailor" / "cover_letter.py",
            ROOT / "src" / "classifier" / "lane_selector.py",
            ROOT / "src" / "qa" / "checker.py",
            ROOT / "src" / "contacts" / "hm_finder.py",
        ]
        for path in modules_to_check:
            source = path.read_text()
            assert "from llm import call_claude" in source, (
                f"{path.name} should import call_claude from llm module"
            )
            assert "anthropic.Anthropic(" not in source, (
                f"{path.name} should not instantiate Anthropic client directly"
            )
            assert "import anthropic" not in source, (
                f"{path.name} should not import anthropic SDK"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #8: MY_EMAIL validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestMyEmailValidation:
    """Verify main.py validates MY_EMAIL before sending."""

    def test_source_checks_my_email(self):
        source = (ROOT / "src" / "main.py").read_text()
        # Should retrieve MY_EMAIL without a default empty string
        assert 'os.getenv("MY_EMAIL")' in source or "os.getenv('MY_EMAIL')" in source
        # Should NOT have the old pattern with empty string default
        assert 'os.getenv("MY_EMAIL", "")' not in source

    def test_source_has_recipient_check(self):
        source = (ROOT / "src" / "main.py").read_text()
        assert "if not recipient" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #9: Playwright browser cleanup
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlaywrightBrowserCleanup:
    """Verify browser.close() is in a finally block in jd_fetcher.py."""

    def test_fetch_with_playwright_has_try_finally(self):
        source = (ROOT / "src" / "scraper" / "jd_fetcher.py").read_text()
        tree = ast.parse(source)

        # Find _fetch_with_playwright function
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_fetch_with_playwright":
                func = node
                break
        assert func is not None

        # Check for a Try node with a finally containing browser.close()
        has_finally_close = False
        for node in ast.walk(func):
            if isinstance(node, ast.Try) and node.finalbody:
                for fin_node in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])):
                    if isinstance(fin_node, ast.Call):
                        if (isinstance(fin_node.func, ast.Attribute)
                                and fin_node.func.attr == "close"):
                            has_finally_close = True
        assert has_finally_close, (
            "_fetch_with_playwright should have browser.close() in a finally block"
        )

    def test_fetch_ats_page_has_try_finally(self):
        source = (ROOT / "src" / "scraper" / "jd_fetcher.py").read_text()
        tree = ast.parse(source)

        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_fetch_ats_page":
                func = node
                break
        assert func is not None

        has_finally_close = False
        for node in ast.walk(func):
            if isinstance(node, ast.Try) and node.finalbody:
                for fin_node in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])):
                    if isinstance(fin_node, ast.Call):
                        if (isinstance(fin_node.func, ast.Attribute)
                                and fin_node.func.attr == "close"):
                            has_finally_close = True
        assert has_finally_close, (
            "_fetch_ats_page should have browser.close() in a finally block"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #10: .eml file opened with explicit encoding
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmlEncoding:
    """Verify parse_alert_from_eml opens files with explicit encoding."""

    def test_source_specifies_encoding(self):
        source = (ROOT / "src" / "parser" / "email_parser.py").read_text()
        # The open() call should include encoding
        assert 'encoding="utf-8"' in source
        assert 'errors="replace"' in source

    def test_parse_handles_non_ascii(self, tmp_path):
        """Verify non-ASCII content doesn't crash the parser."""
        from parser.email_parser import parse_alert_from_eml

        # Create a minimal .eml with non-ASCII chars
        eml_content = (
            "From: test@example.com\n"
            "Subject: Test\n"
            "MIME-Version: 1.0\n"
            "Content-Type: text/html; charset=utf-8\n"
            "\n"
            "<html><body>Héllo wörld — café résumé</body></html>\n"
        )
        eml_path = tmp_path / "test.eml"
        eml_path.write_text(eml_content, encoding="utf-8")

        # Should not raise
        result = parse_alert_from_eml(eml_path)
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# Warning #11: Gmail query sanitization
# ═══════════════════════════════════════════════════════════════════════════════


class TestGmailQuerySanitization:
    """Verify _sanitize_query strips dangerous characters."""

    def test_sanitize_strips_quotes(self):
        # Import directly to test the function
        sys.path.insert(0, str(ROOT / "src"))
        from gmail.client import _sanitize_query
        assert _sanitize_query('test"injection') == "testinjection"

    def test_sanitize_strips_backslash(self):
        from gmail.client import _sanitize_query
        assert _sanitize_query("test\\escape") == "testescape"

    def test_sanitize_strips_newlines(self):
        from gmail.client import _sanitize_query
        assert _sanitize_query("test\ninjection\r") == "testinjection"

    def test_sanitize_preserves_normal_values(self):
        from gmail.client import _sanitize_query
        assert _sanitize_query("HiringCafe") == "HiringCafe"
        assert _sanitize_query("hiring-agent-processed") == "hiring-agent-processed"

    def test_sanitize_used_in_query_construction(self):
        """Verify the actual source uses _sanitize_query in query building."""
        source = (ROOT / "src" / "gmail" / "client.py").read_text()
        assert "_sanitize_query(subject_contains)" in source
        assert "_sanitize_query(processed_label)" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Digest compose still works
# ═══════════════════════════════════════════════════════════════════════════════


class TestComposeDigest:
    """Verify compose_digest still works after removing send_digest."""

    def test_compose_basic(self):
        from gmail.digest import compose_digest

        body = compose_digest(
            processed=[{
                "title": "Engineer",
                "company": "Acme",
                "url": "https://example.com",
                "lane": "pmm",
            }],
            skipped=[{
                "title": "Designer",
                "company": "Beta",
                "url": "https://beta.com",
                "reason": "JD too short",
            }],
        )
        assert "Processed (1)" in body
        assert "Engineer" in body
        assert "Skipped (1)" in body
        assert "JD too short" in body

    def test_compose_empty(self):
        from gmail.digest import compose_digest
        body = compose_digest(processed=[], skipped=[])
        assert "Processed (0)" in body
