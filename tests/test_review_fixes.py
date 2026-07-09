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
import inspect
import os
import re
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

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


def _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config,
                                send_raises=False,
                                my_email="test@example.com",
                                clear_my_email=False):
    """Drive main.main() end-to-end with a stubbed Gmail client and stubbed
    pipeline. Returns the gmail stub for assertions.

    Every I/O boundary the digest-send branch depends on is patched:
      - main.GmailClient        — no real Gmail auth
      - gmail.find_unprocessed_alert — returns a fake alert dict
      - main.parse_alert_email  — returns a single fake job
      - main.run_pipeline       — returns 1 processed job, 0 skipped
      - main.ROOT / "output"    — redirected to tmp_path so no repo write
      - MY_EMAIL env            — set to my_email (empty string OK), or
                                  fully DELETED when clear_my_email=True.
                                  Distinguishes MY_EMAIL='' from
                                  MY_EMAIL-unset — main.py handles both
                                  via `if not recipient`, so tests must
                                  cover both branches independently.
      - sys.argv                — production mode (no --test / --dry-run)
    """
    # Phase 5 iter-2 (finding #8): removed the raw `sys.path.insert(0, ...)`
    # that was here — it's redundant with the module-scope prepend at the
    # top of this file and would leak past the test if monkeypatch weren't
    # restoring sys.path. Import directly.
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
    # Fixture also forces apply.enabled=false so the digest-send branch
    # this helper exercises stays on the plain-str (non-DigestPayload)
    # path regardless of any future settings.yaml flip.
    main_root_with_config(monkeypatch, tmp_path)

    # Phase 5 iter-2 (my_email='' semantic gap): distinguish 'empty string'
    # from 'variable deleted'. If clear_my_email=True, delete entirely; else
    # setenv with the given value (which may itself be ''). Both branches
    # exist in prod (unset vs blank), and `if not recipient` treats them
    # identically — but tests must cover BOTH so a future hardening to
    # `if recipient is None` would not silently regress the empty-string
    # branch.
    if clear_my_email:
        monkeypatch.delenv("MY_EMAIL", raising=False)
    else:
        monkeypatch.setenv("MY_EMAIL", my_email)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    main_mod.main()
    return gmail


class TestDigestSendAndMarkProcessed:
    """H13 behavioral: exercise main.main()'s real digest-send branch."""

    def test_mark_processed_called_on_success(self, monkeypatch, tmp_path, main_root_with_config):
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config, send_raises=False)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_called_once_with(
            "msg_123", "hiring-agent-processed"
        )

    def test_mark_processed_not_called_on_send_failure(self, monkeypatch, tmp_path, main_root_with_config):
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config, send_raises=True)
        gmail.send_digest.assert_called_once()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_not_called_when_email_unset(self, monkeypatch, tmp_path, main_root_with_config):
        """MY_EMAIL not present in the environment at all."""
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config, clear_my_email=True)
        gmail.send_digest.assert_not_called()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_not_called_when_email_empty_string(self, monkeypatch, tmp_path, main_root_with_config):
        """MY_EMAIL='' (present but blank) — must ALSO short-circuit the
        send. Distinct from the unset case: guards against a future
        hardening to `if recipient is None:` silently regressing the
        empty-string branch."""
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config, my_email="")
        gmail.send_digest.assert_not_called()
        gmail.mark_processed.assert_not_called()

    def test_mark_processed_ordered_after_send_digest(self, monkeypatch, tmp_path, main_root_with_config):
        """The send must resolve BEFORE the mark. If main.py ever reorders
        mark_processed above send_digest inside the same try (a mutation the
        old MagicMock self-test missed), this assertion fires."""
        gmail = _drive_main_with_gmail_stub(monkeypatch, tmp_path, main_root_with_config, send_raises=False)
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
    node list, WITHOUT descending into nested Try nodes.

    Phase 5 iter-2/iter-3 (H14 finding): a naive `ast.walk(n)` per statement
    descends into the body / handlers / finalbody of every nested Try —
    so a `mark_processed` call in a NESTED except handler would be
    counted as a call in the OUTER try's body, silently bypassing the
    'no mark_processed in an except handler' guard in the H14 caller.

    Boundary-aware walk: iterate nodes with `ast.iter_child_nodes` and
    skip descent into any child that is itself an ast.Try — its inner
    calls belong to that inner Try, not the enclosing one.

    iter-3 CRITICAL: the child-only guard is NOT sufficient. When a top-
    level `n` in `nodes` is ITSELF an ast.Try, `_walk_no_try(n)` was
    invoked directly (not through `iter_child_nodes`), so it recursed
    into that Try's own body + handlers + finalbody + orelse — leaking
    the nested Try's calls into the outer Try's tally. Guard both entry
    points: skip nested Try nodes at the top-level loop AND during child
    descent.
    """
    lines: list[int] = []

    def _walk_no_try(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr:
                lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Try):
                # Nested Try — its calls belong to it, not the caller.
                continue
            _walk_no_try(child)

    for n in nodes:
        # iter-3 fix: a top-level Try in `nodes` is a nested Try relative
        # to the caller who owns `nodes`. Its calls belong to that inner
        # Try, not the enclosing one — same rule the child-descent guard
        # enforces.
        if isinstance(n, ast.Try):
            continue
        _walk_no_try(n)
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


class TestFindCallsInWalkerBoundaries:
    """iter-3 CRITICAL regression: `_find_calls_in` must NOT descend into
    a Try whose calls belong to that Try, not the caller. Applies both
    when the Try is a CHILD of a walked node AND when the Try is a
    TOP-LEVEL element of `nodes`.

    A bug in either guard would leak nested-Try calls into the outer
    tally — silently defeating the H14 'no mark_processed in an except'
    check on real main.py code.
    """

    def test_top_level_try_in_nodes_is_not_descended(self):
        """Directly exercise the top-level guard: pass a nested Try as
        an element of `nodes` and confirm its own body's calls are NOT
        counted. Mutation: remove the `if isinstance(n, ast.Try): continue`
        guard → this test fails (call gets counted)."""
        src = textwrap.dedent("""
            try:
                inner_call.mark_processed()
            except Exception:
                pass
        """)
        tree = ast.parse(src)
        # tree.body is [Try] — pass the Try node in as a top-level element.
        lines = _find_calls_in(tree.body, "mark_processed")
        assert lines == [], (
            f"_find_calls_in must skip a top-level Try in `nodes`; got "
            f"lines={lines}. The bug lets the Try's inner call leak into "
            f"the caller's tally, silently defeating the H14 guard."
        )

    def test_nested_try_as_child_is_not_descended(self):
        """Regression for the original child-descent guard (iter-2 fix).
        Ensures we did not lose that guard while adding the iter-3 top-
        level guard. Mutation: remove `if isinstance(child, ast.Try):
        continue` in the recursion → this test fails."""
        src = textwrap.dedent("""
            def outer():
                try:
                    ok_call()
                    try:
                        nested_call.mark_processed()
                    except Exception:
                        pass
                except Exception:
                    pass
        """)
        tree = ast.parse(src)
        # Grab the outer function's body — its first statement is a Try.
        outer_fn = tree.body[0]
        outer_try = outer_fn.body[0]
        # Pass the outer Try's own body — the ok_call is there, plus a
        # nested Try whose inner mark_processed must NOT count.
        lines = _find_calls_in(outer_try.body, "mark_processed")
        assert lines == [], (
            f"_find_calls_in must skip a nested Try during child descent; "
            f"got lines={lines}."
        )

    def test_direct_call_in_top_level_expression_is_counted(self):
        """Sanity: the top-level guard must NOT be over-broad. A direct
        Expr with a Call at the top level must still be counted."""
        src = "top_call.mark_processed()\n"
        tree = ast.parse(src)
        lines = _find_calls_in(tree.body, "mark_processed")
        assert len(lines) == 1, f"expected 1 direct call, got {lines}"


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
        shape — for defense in depth.

        Phase 5 iter-2 (finding #10): the previous `sendgrid.net in url
        or hiring.cafe in url` OR-branch made the hiring.cafe alternative
        dead — every URL in the shipped fixture is a SendGrid tracking
        link, so the second disjunct was unreachable and silently masked
        a regression that swapped the URL host to something else entirely.
        Tightened to a single `sendgrid.net in url` assertion — the actual
        fixture shape."""
        from parser.email_parser import parse_alert_from_eml
        jobs = parse_alert_from_eml(
            ROOT / "test_data" / "sample_alert.eml",
            max_jobs=99,
        )
        for j in jobs:
            url = j.get("url", "")
            assert url, f"job {j.get('title')!r} has no URL"
            assert "sendgrid.net" in url, (
                f"job {j.get('title')!r} url does not look like the "
                f"SendGrid tracking shape: {url!r}"
            )


class TestNoopGmailClientContract:
    """Phase 5 iter-2/iter-3 (M17 finding): _NoopGmailClient is the stub
    main() passes to run_pipeline in --test mode. If its method signatures
    drift from the real GmailClient, --test mode crashes with AttributeError
    / TypeError the moment the S17 seam invokes a mismatched stub — but
    every existing test that hits main() in --test mode ALSO stubs
    run_pipeline, so the drift is invisible.

    Three guards:
      1. `_METHODS` covers every stub method the SEAM invokes at runtime.
         iter-3 added the source-grep check because a new seam call added
         without extending _METHODS would crash --test mode but leave the
         contract test green (the drift bug's mirror image).
      2. Signature shape matches the real GmailClient (kw-only markers,
         arity, param names).
      3. Every method is actually callable on an instance with a plausible
         arg set.

    (2)+(3) are combined into ONE loop in iter-3 to avoid the split-drift
    hazard flagged as finding #8: two tests iterating the same _METHODS
    tuple can silently disagree about the contract (e.g. one adds a method
    the other doesn't).

    Mutation checks:
      - revert _NoopGmailClient.search to `def search(self, query)` → sig fails
      - add a new `gmail.foo()` seam call without adding 'foo' to _METHODS
        → seam-coverage check fails
    """

    _METHODS = (
        "search",
        "get_or_create_label",
        "send_with_labels",
        "apply_label",
        "remove_label",
        "reply_to_thread",
    )

    def test_seam_gmail_methods_all_covered_by_METHODS(self):
        """iter-3 (finding #3): if the seam adds a new `gmail.<method>()`
        call and _METHODS doesn't stub it, --test mode crashes but the
        signature/callable tests both stay green (they only iterate what
        _METHODS already lists — the mirror image of the M17 drift bug).

        Grep every `gmail.<method>(...)` call site in the SEAM (src/apply/*)
        — the code path that receives _NoopGmailClient in --test mode.
        (src/main.py invokes methods on the REAL GmailClient directly in
        the production branch, not on the stub, so those are out of scope
        for the stub's contract.)
        """
        # Seam-only: files whose `gmail` parameter binds to _NoopGmailClient
        # in --test mode. src/main.py's own `gmail = GmailClient()` calls are
        # a different code path (real Gmail auth, production-only).
        seam_files = [
            ROOT / "src" / "apply" / "review.py",
            ROOT / "src" / "apply" / "_seam.py",
        ]
        # Match `gmail.foo(` — the seam's binding name for the injected
        # client. Skips comment lines to avoid docstring-attribute noise.
        call_pattern = re.compile(r"\bgmail\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
        called = set()
        for path in seam_files:
            source = path.read_text()
            for line in source.splitlines():
                if line.strip().startswith("#"):
                    continue
                for m in call_pattern.finditer(line):
                    called.add(m.group(1))

        assert called, (
            "Seam grep returned zero gmail.<method>() calls — the seam "
            "files were probably renamed. Update seam_files in this test."
        )

        missing = called - set(self._METHODS)
        assert not missing, (
            f"Seam invokes gmail methods NOT covered by _NoopGmailClient "
            f"stub / _METHODS: {sorted(missing)}. --test mode will crash "
            f"AttributeError the first time these run. Add them to "
            f"_NoopGmailClient + _METHODS."
        )

        # And the reverse: catch a dead _METHODS entry the seam no longer
        # calls (would be code-hygiene noise, not a bug — but flag it).
        stale = set(self._METHODS) - called
        assert not stale, (
            f"_METHODS lists methods no longer called by the seam: "
            f"{sorted(stale)}. Remove from _METHODS + _NoopGmailClient."
        )

    def test_noop_gmail_client_matches_real_and_is_callable(self):
        """Combined signature + callability contract. Consolidated from two
        tests in iter-2 (finding #8) — the split iterated the same _METHODS
        tuple twice, and could silently disagree about the contract.

        For every _METHOD:
          - stub + real must both have the method
          - stub sig matches real sig (name + kind per param)
          - stub method is invokable on an instance
          - stub's return value satisfies the seam's expected shape
        """
        from main import _NoopGmailClient
        from gmail.client import GmailClient

        stub_cls = _NoopGmailClient
        real_cls = GmailClient
        stub = stub_cls()

        # Callable exemplar args per method — the seam's actual call shape.
        # Kept explicit so any signature drift is caught by the .invocation,
        # not just introspection.
        callable_probes = {
            "search": (lambda: stub.search("q"), lambda: stub.search("q", max_results=5)),
            "get_or_create_label": (lambda: stub.get_or_create_label("L"),),
            "send_with_labels": (lambda: stub.send_with_labels(subject="s", body="b", to="t"),),
            "apply_label": (lambda: stub.apply_label("m", "L"),),
            "remove_label": (lambda: stub.remove_label("m", "L"),),
            "reply_to_thread": (lambda: stub.reply_to_thread("t", "b"),),
        }
        return_shape = {
            "search": lambda r: r == [],
            "get_or_create_label": lambda r: r == "stub:L",
            "send_with_labels": lambda r: isinstance(r, tuple) and len(r) == 2,
            "apply_label": lambda r: r is None,
            "remove_label": lambda r: r is None,
            "reply_to_thread": lambda r: isinstance(r, str),
        }

        for name in self._METHODS:
            assert hasattr(stub_cls, name), f"_NoopGmailClient missing method {name!r}"
            assert hasattr(real_cls, name), f"GmailClient missing method {name!r} — test is stale?"

            stub_sig = inspect.signature(getattr(stub_cls, name))
            real_sig = inspect.signature(getattr(real_cls, name))
            stub_shape = [(p.name, p.kind) for p in stub_sig.parameters.values()]
            real_shape = [(p.name, p.kind) for p in real_sig.parameters.values()]
            assert stub_shape == real_shape, (
                f"_NoopGmailClient.{name} signature drift vs GmailClient:\n"
                f"  stub: {stub_shape}\n"
                f"  real: {real_shape}"
            )

            # Callable + return-shape checks. Any mismatched positional-
            # only marker or default surfaces here as TypeError.
            probes = callable_probes.get(name, ())
            assert probes, f"iter-3: add a callable probe for _METHOD {name!r}"
            for probe in probes:
                result = probe()
                assert return_shape[name](result), (
                    f"_NoopGmailClient.{name} returned {result!r} — does not "
                    f"satisfy the shape the seam depends on."
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

    def test_broken_url_card_does_not_claim_dedup_slot(self):
        """A3 regression (Phase 5 xhigh iter-1 finding): a card whose
        apply_link is missing/broken must NOT claim the dedup slot for
        (title, company). If it does, the next valid card with the same
        (title, company) is silently dropped — the exact class of bug
        M7 was written to prevent, reintroduced.

        Mutation check: revert email_parser.py to `seen_title_company.add()`
        BEFORE the apply_link guard. This test then fails: len(jobs) == 0
        instead of 1, because the broken-URL first card claims the slot
        and the valid-URL second card is dropped.
        """
        from parser.email_parser import parse_alert_email
        # Card 1: same (title, company) as card 2 but NO <a Apply> link.
        # Card 2: same (title, company) with a valid apply URL — must
        # survive because card 1 never claimed the slot.
        html = (
            "<html><body><table>"
            # Card 1 — broken: no apply link
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>Acme — Remote</div>"
            "</td>"
            # Card 2 — valid: with apply link
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>Acme — Remote</div>"
            '<a href="https://example.com/valid">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1, (
            f"Expected 1 job (broken-URL card must not claim dedup slot), "
            f"got {len(jobs)}. Broken-URL card claimed the slot and dropped "
            f"the valid card — reintroduces the M7-class bug."
        )
        assert jobs[0]["url"] == "https://example.com/valid"

    def test_unknown_company_does_not_collide_across_jobs(self):
        """(title, 'Unknown') collision (Phase 5 xhigh iter-1 finding): when
        company parsing falls back to 'Unknown' for two distinct cards with
        the same title, dedup keyed on (title, 'Unknown') silently drops
        one — the same audit-flagged failure the M7 fix was meant to
        prevent. Distinct URLs identify distinct jobs; both must surface.

        Mutation check: remove the URL-fallback branch from the parser
        (revert to a bare `(title, company)` key). This test then fails:
        one of the two Unknown-company jobs is dropped.
        """
        from parser.email_parser import parse_alert_email
        # Two cards whose company <div> lacks the em-dash split shape —
        # parser falls back to 'Unknown' for both. Distinct apply URLs
        # prove they are distinct jobs.
        html = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div></div>"  # no company text → falls back to 'Unknown'
            '<a href="https://example.com/one">Apply</a>'
            "</td>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div></div>"
            '<a href="https://example.com/two">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 2, (
            f"Expected 2 jobs when company fell back to 'Unknown' for "
            f"both cards (distinct URLs → distinct jobs), got {len(jobs)}"
        )
        urls = sorted(j["url"] for j in jobs)
        assert urls == [
            "https://example.com/one",
            "https://example.com/two",
        ], f"Expected both distinct URLs preserved, got {urls}"

    def test_empty_string_company_does_not_collide_across_jobs(self):
        """(title, '') empty-string sentinel collision (Phase 5 xhigh iter-2
        CRITICAL finding): when the company <div>'s raw text STARTS with an
        em-dash (e.g. '— Remote'), the em-dash split path produces
        parts[0].strip() = '' (empty string) — NOT 'Unknown'. The iter-1
        fix only added a URL-fallback for company == 'Unknown', so two
        cards with raw='— Remote' both key to (title, '') and the second
        is silently dropped. Same M7 failure class, different sibling
        sentinel.

        Mutation check: replace the sentinel guard in email_parser.py with
        the iter-1-only `company == "Unknown"` check. This test then
        fails: len(jobs) == 1 because both cards key to (title, '') and
        the second is dropped.
        """
        from parser.email_parser import parse_alert_email
        # Two cards where company_div's raw text starts with em-dash —
        # split path produces empty-string company. Distinct URLs → distinct
        # jobs. Both must surface, not just the first.
        html = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>— Remote</div>"  # split → company='', location='Remote'
            '<a href="https://example.com/one">Apply</a>'
            "</td>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>— Remote</div>"
            '<a href="https://example.com/two">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 2, (
            f"Expected 2 jobs when company fell back to empty string for "
            f"both cards (distinct URLs → distinct jobs), got {len(jobs)}. "
            f"Second card silently dropped by (title, '') collision — the "
            f"empty-string sibling of the iter-1 'Unknown' bug."
        )
        urls = sorted(j["url"] for j in jobs)
        assert urls == [
            "https://example.com/one",
            "https://example.com/two",
        ], f"Expected both distinct URLs preserved, got {urls}"

    def test_en_dash_empty_string_company_does_not_collide(self):
        """En-dash sibling of the em-dash empty-string case. Parser handles
        both '—' (em) and '–' (en) via the same split-then-strip path, so
        a card with raw='– Remote' has the same empty-string sentinel
        failure. Sweep coverage for the sentinel-collision class."""
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Content Manager</span></h3>"
            "<div>– Remote</div>"  # en-dash split → company=''
            '<a href="https://example.com/en1">Apply</a>'
            "</td>"
            "<td><h3><span>Content Manager</span></h3>"
            "<div>– Remote</div>"
            '<a href="https://example.com/en2">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 2, (
            f"En-dash empty-company sibling of the em-dash bug: expected "
            f"2 jobs, got {len(jobs)}. Same sentinel-collision class."
        )


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
