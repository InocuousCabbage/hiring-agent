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
# Critical #2 (H13 + H14): mark_processed only called on send success
#
# ── Failure-mode being guarded ──
# main.main()'s digest-send branch runs (roughly):
#     try:
#         gmail.send_digest(...)
#         gmail.mark_processed(alert_id, processed_label)
#     except Exception as exc:
#         log.error(...)
# The shipped bug this guards against: mark_processed OUTSIDE the try (or
# BEFORE the send inside it) — either arrangement can mark an alert
# 'processed' when the digest never actually shipped to the operator's inbox,
# and the alert never re-enters the pipeline on the next cron tick.
#
# ── Discipline: BEHAVIORAL, not source-grep ──
# The previous H13/H14 tests re-implemented main.py's send/mark logic against
# a MagicMock (H13) or asserted `"mark_processed" in line` (H14 tautology).
# Neither would fail if main.py was reintroduced with the bug. The
# replacements below drive main.main() end-to-end with a stub GmailClient,
# then assert mark_processed's presence-vs-absence and ordering against the
# real production call site.
# ═══════════════════════════════════════════════════════════════════════════════


def _drive_main_with_gmail_stub(monkeypatch, tmp_path, send_raises=False,
                                my_email="test@example.com"):
    """Drive main.main() end-to-end with a stubbed Gmail client and stubbed
    pipeline. Returns the gmail stub for assertions.

    Every I/O boundary the digest-send branch depends on is patched:
      - main.GmailClient        — no real Gmail auth
      - gmail.find_unprocessed_alert — returns a fake alert dict
      - main.parse_alert_email  — returns a single fake job
      - main.run_pipeline       — returns 1 processed job, 0 skipped
      - main.ROOT / "output"    — redirected to tmp_path so no repo write
      - MY_EMAIL env            — set or cleared per case
      - sys.argv                — production mode (no --test / --dry-run)
    """
    # Ensure src/ is importable (redundant with module-level insert, but
    # keeps this helper self-contained for readers).
    sys.path.insert(0, str(ROOT / "src"))
    import main as main_mod
    import gmail.client as gmail_client_mod  # GmailClient is imported inside main()

    gmail = MagicMock()
    if send_raises:
        gmail.send_digest.side_effect = Exception("SMTP timeout")
    gmail.find_unprocessed_alert.return_value = {
        "id": "msg_123",
        "html": "<html></html>",
        "text": "",
    }

    # main() does `from gmail.client import AuthError, GmailClient` at call time,
    # so we patch the source module (main.py has no module-scope GmailClient).
    monkeypatch.setattr(gmail_client_mod, "GmailClient", lambda: gmail)
    monkeypatch.setattr(
        main_mod,
        "parse_alert_email",
        lambda html_body, text_body, max_jobs: [
            {"title": "Engineer", "company": "Acme", "url": "https://example.com"}
        ],
    )
    processed_fixture = [{
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com",
        "lane": "pmm",
        "resume_pdf": None,
        "resume_docx": tmp_path / "acme_resume.docx",
        "cover_letter_pdf": None,
        "cover_letter_docx": tmp_path / "acme_cl.docx",
        "hiring_manager": None,
        "apply_result": None,
    }]
    # Materialize the docx paths so _build_attachments has real files.
    (tmp_path / "acme_resume.docx").write_bytes(b"PK")  # ZIP-ish magic
    (tmp_path / "acme_cl.docx").write_bytes(b"PK")

    def _fake_run_pipeline(**kwargs):
        return (processed_fixture, [], [])
    monkeypatch.setattr(main_mod, "run_pipeline", _fake_run_pipeline)

    # Redirect ROOT/output writes to tmp_path so we never touch the repo.
    monkeypatch.setattr(main_mod, "ROOT", tmp_path)
    # main.load_config uses ROOT to resolve settings.yaml — put a symlink
    # (or reuse the real file via the original ROOT).
    real_root = Path(__file__).parent.parent
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        (real_root / "config" / "settings.yaml").read_text()
    )
    (tmp_path / "templates" / "project_bank.yaml").write_text(
        (real_root / "templates" / "project_bank.yaml").read_text()
    )

    if my_email:
        monkeypatch.setenv("MY_EMAIL", my_email)
    else:
        monkeypatch.delenv("MY_EMAIL", raising=False)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    main_mod.main()
    return gmail


class TestDigestSendAndMarkProcessed:
    """H13 behavioral: exercise main.main()'s real digest-send branch."""

    def test_mark_processed_called_on_success(self, monkeypatch, tmp_path):
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, send_raises=False)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_called_once_with(
            "msg_123", "hiring-agent-processed"
        )

    def test_mark_processed_not_called_on_send_failure(self, monkeypatch, tmp_path):
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, send_raises=True)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_not_called_when_email_missing(self, monkeypatch, tmp_path):
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, my_email="")
        gmail.send_digest.assert_not_called()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_ordered_after_send_digest(self, monkeypatch, tmp_path):
        """The send must resolve BEFORE the mark. If main.py ever reorders
        mark_processed above send_digest inside the same try (a mutation the
        old MagicMock self-test missed), this assertion fires."""
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, send_raises=False)
        # Filter to just the two calls we care about, in order.
        call_names = [
            c[0] for c in gmail.mock_calls
            if c[0] in ("send_digest", "mark_processed")
        ]
        assert call_names == ["send_digest", "mark_processed"], (
            f"send_digest must precede mark_processed; got: {call_names}"
        )


def _find_calls_in(nodes, attr: str) -> list[int]:
    """Return line numbers of every Call to `.{attr}(...)` inside the AST
    node list. Uses ast.walk so nested statements (for/if/…) are covered."""
    lines = []
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr == attr:
                    lines.append(sub.lineno)
    return lines


class TestSendDigestMarkProcessedStructurallyPaired:
    """H14 structural (replaces the substring-in-line tautology).

    Walks the AST of main.main(). For every Try node whose body contains a
    gmail.send_digest() call, we verify:
      1. mark_processed also lives in the SAME Try.body (not handlers/finalbody)
      2. mark_processed appears AFTER send_digest (line order)
      3. No mark_processed sneaks into an `except` handler for that Try

    Mutations this catches (any one FAILS the test):
      - Move mark_processed OUT of the try (bare top-level statement) → (1) fails
      - Reorder so mark_processed comes BEFORE send_digest inside the try → (2) fails
      - Move mark_processed into the `except` handler → (1)+(3) fail

    (The mutation "reorder within same try body" is ALSO caught by the H13
    behavioral test's `test_mark_processed_ordered_after_send_digest`, so
    we have defense in depth: structural + behavioral.)

    Standalone mark_processed calls in main() that are NOT paired with a
    send_digest inside the same Try are legitimate (the no-jobs-found branch
    marks the alert processed without a digest ship). This test only
    constrains the send+mark pair — that is the failure mode Critical #2
    was written to prevent.
    """

    def test_send_and_mark_share_try_body_with_correct_order(self):
        source = (ROOT / "src" / "main.py").read_text()
        tree = ast.parse(source)
        main_func = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "main"), None,
        )
        assert main_func is not None, "main() function not found"

        paired = 0
        for node in ast.walk(main_func):
            if not isinstance(node, ast.Try):
                continue
            send_lines = _find_calls_in(node.body, "send_digest")
            if not send_lines:
                continue  # not the try we care about
            mark_body_lines = _find_calls_in(node.body, "mark_processed")
            assert mark_body_lines, (
                f"Try containing send_digest at lines {send_lines} has NO "
                f"mark_processed in its body — the guard is defeated. "
                f"mark_processed must live in the same Try.body so a "
                f"send_digest exception skips it."
            )
            # Assert every mark_processed follows every send_digest by line
            assert min(mark_body_lines) > max(send_lines), (
                f"mark_processed at line {min(mark_body_lines)} must FOLLOW "
                f"send_digest at line {max(send_lines)} (send-then-mark)."
            )
            # And no mark_processed hiding in except handlers.
            for handler in node.handlers:
                handler_marks = _find_calls_in([handler], "mark_processed")
                assert not handler_marks, (
                    f"mark_processed at line {handler_marks[0]} is inside an "
                    f"except handler of the send_digest try — it would fire "
                    f"even when send_digest raised, defeating the guard."
                )
            paired += 1

        assert paired >= 1, (
            "expected at least one Try that pairs send_digest with mark_processed"
        )


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

    def test_no_send_digest_residue(self):
        """The removed send_digest used Gmail API/MIME plumbing — none of that
        should remain in digest.py. Path import is now legitimately needed by
        compose_digest to introspect attachment suffixes for the body note
        (PDF + DOCX vs DOCX-only), so it is no longer forbidden."""
        source = (ROOT / "src" / "gmail" / "digest.py").read_text()
        assert "MIMEText" not in source
        assert "MIMEMultipart" not in source
        assert "google.auth" not in source
        assert "googleapiclient" not in source
        # send_digest itself stays gone
        assert "def send_digest" not in source


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
# M20: email_parser golden-file extraction (was: isinstance(result, list) only)
#
# The prior TestEmlEncoding.test_parse_handles_non_ascii asserted only that
# the parser returned SOME list — even []. It was the ONLY behavioral test
# for email_parser, and nothing verified that title/company/url were
# actually extracted from the shipped sample_alert.eml fixture.
#
# These golden assertions lock in the extraction contract: exact job count,
# the specific title/company pairs the fixture is known to contain, and the
# SendGrid tracking URL shape. A parser regression that swaps <h3>/<div>
# selectors, breaks the em-dash split, or returns [] on a valid alert will
# fail here.
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmailParserGoldenFromSampleAlert:
    """Golden assertions against the checked-in sample_alert.eml fixture.

    The fixture is a real Hiring.cafe alert with 6 distinct jobs. If the
    fixture is edited, update SAMPLE_ALERT_GOLDEN below to match — but do
    NOT relax the assertions to `> 0` or `isinstance(..., list)`.
    """

    SAMPLE_ALERT_GOLDEN = [
        ("Lead Product Marketing Manager", "Group O"),
        ("Sr. Specialist, Product and Solutions Marketing", "Cardinal Health"),
        ("Senior Product Marketing Manager – Financial Close", "OneStream"),
        ("Product Managers #IN1176", "Cummins"),
        ("Senior Marketing Manager - Global Digital Experience", "Sinch"),
        ("Product Specialist Demand Generation (BIM, Remote, USA)", "Allplan"),
    ]

    def test_sample_alert_job_count_matches_golden(self):
        from parser.email_parser import parse_alert_from_eml
        jobs = parse_alert_from_eml(
            ROOT / "test_data" / "sample_alert.eml",
            max_jobs=99,
        )
        assert len(jobs) == len(self.SAMPLE_ALERT_GOLDEN), (
            f"Expected {len(self.SAMPLE_ALERT_GOLDEN)} jobs from sample_alert.eml, "
            f"got {len(jobs)}. If the fixture was intentionally edited, update "
            f"SAMPLE_ALERT_GOLDEN. Otherwise the parser regressed."
        )

    def test_sample_alert_title_company_pairs_match_golden(self):
        from parser.email_parser import parse_alert_from_eml
        jobs = parse_alert_from_eml(
            ROOT / "test_data" / "sample_alert.eml",
            max_jobs=99,
        )
        actual = [(j["title"], j["company"]) for j in jobs]
        assert actual == self.SAMPLE_ALERT_GOLDEN, (
            f"title/company extraction drifted from golden:\n"
            f"  expected: {self.SAMPLE_ALERT_GOLDEN}\n"
            f"  got:      {actual}"
        )

    def test_sample_alert_every_job_has_sendgrid_tracking_url(self):
        """Every card in the shipped fixture routes through SendGrid; the
        parser must surface a non-empty URL for each. Vacuous "url exists"
        checks (`assert j.get("url")`) would still pass on the empty string,
        so we also assert the SendGrid host prefix — the actual fixture
        shape — for defense in depth."""
        from parser.email_parser import parse_alert_from_eml
        jobs = parse_alert_from_eml(
            ROOT / "test_data" / "sample_alert.eml",
            max_jobs=99,
        )
        for j in jobs:
            url = j.get("url", "")
            assert url, f"job {j.get('title')!r} has no URL"
            assert "sendgrid.net" in url or "hiring.cafe" in url, (
                f"job {j.get('title')!r} url does not look like the "
                f"SendGrid tracking shape: {url!r}"
            )


class TestEmailParserDedupByTitleAndCompany:
    """M7 regression: dedup keys on (title, company), not title alone.

    Prior bug: `if title in seen_titles: continue` silently dropped the
    second card whenever two alerts contained the same job title at
    different companies (a common shape for "Product Marketing Manager"
    or "Content Marketing Manager"). No log line was emitted, so the
    dropped job was invisible.

    Fix: dedup key = (title, company) after company extraction. This test
    exercises the fix with a synthetic 2-card HTML fragment; if the fix
    regresses, only 1 job is returned and this test fails.
    """

    def _make_alert_html(self, cards: list[tuple[str, str]]) -> str:
        """Build minimal HTML that mirrors the Hiring.cafe card shape:
        an <h3><span>Title</span></h3> followed by a <div>Company — Location</div>
        inside a <td>."""
        card_html = "".join(
            f'<td><h3><span>{title}</span></h3>'
            f'<div>{company} — Remote</div>'
            f'<a href="https://example.com/{i}">Apply</a></td>'
            for i, (title, company) in enumerate(cards)
        )
        return f"<html><body><table>{card_html}</table></body></html>"

    def test_same_title_different_companies_returns_two_jobs(self):
        from parser.email_parser import parse_alert_email
        html = self._make_alert_html([
            ("Product Marketing Manager", "Company A"),
            ("Product Marketing Manager", "Company B"),
        ])
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 2, (
            f"Expected 2 jobs (same title, different companies), got "
            f"{len(jobs)}. Jobs: {[(j['title'], j['company']) for j in jobs]}"
        )
        companies = sorted(j["company"] for j in jobs)
        assert companies == ["Company A", "Company B"], (
            f"Expected both companies represented, got {companies}"
        )

    def test_true_duplicates_still_deduped(self):
        """Same (title, company) pair should still be deduped — the fix
        narrows the key, it does not remove dedup entirely."""
        from parser.email_parser import parse_alert_email
        html = self._make_alert_html([
            ("Product Marketing Manager", "Acme"),
            ("Product Marketing Manager", "Acme"),  # exact duplicate
            ("Product Marketing Manager", "Beta"),
        ])
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        # 2 unique (title, company) pairs after dedup
        pairs = sorted((j["title"], j["company"]) for j in jobs)
        assert pairs == [
            ("Product Marketing Manager", "Acme"),
            ("Product Marketing Manager", "Beta"),
        ], f"Expected exactly 2 unique pairs, got {pairs}"


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
