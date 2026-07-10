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
    node list, WITHOUT descending into nested Try / TryStar nodes.

    Phase 5 iter-2/iter-3 (H14 finding): a naive `ast.walk(n)` per statement
    descends into the body / handlers / finalbody of every nested Try —
    so a `mark_processed` call in a NESTED except handler would be
    counted as a call in the OUTER try's body, silently bypassing the
    'no mark_processed in an except handler' guard in the H14 caller.

    Boundary-aware walk: skip descent into any node that is itself an
    ast.Try (or ast.TryStar, the PEP 654 `except*` construct in Python
    3.11+ — same descent hazard as ast.Try; the file targets Python 3.14
    per pyproject). The caller's tally must never include nested-Try
    calls, regardless of which Try flavor the nesting uses.

    PR #12 finding #2 (unify the Try-skip): a single `if isinstance(node,
    (ast.Try, ast.TryStar)): return` at the top of `_walk_no_try`
    enforces the rule for BOTH entry points at once — the top-level
    loop calls `_walk_no_try(n)` directly, and the child-descent loop
    calls `_walk_no_try(child)`. The prior two-guard shape (once at the
    top-level loop, once inside the child-descent loop) split the
    invariant across two sites that could drift.
    """
    lines: list[int] = []

    def _walk_no_try(node):
        # PR #12 findings #1 + #2: single choke point for the Try-skip
        # rule. Any nested Try / TryStar returns immediately, so no
        # code path descends into a Try body/handlers/finalbody/orelse
        # from an outer walk. Applied uniformly whether `node` came
        # from `nodes` at the top level or from `ast.iter_child_nodes`
        # during descent.
        if isinstance(node, (ast.Try, ast.TryStar)):
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr:
                lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            _walk_no_try(child)

    for n in nodes:
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

    def test_top_level_try_star_in_nodes_is_not_descended(self):
        """PR #12 finding #1: `except*` (PEP 654) constructs parse into
        `ast.TryStar` nodes, NOT `ast.Try`. The guard must skip both.

        Pre-fix (guard only `ast.Try`): a `mark_processed` call inside
        an `except*` body leaks into the caller's tally, silently
        defeating the H14 rule the same way an `ast.Try` leak would.
        The codebase targets Python 3.14 (see pyproject) so `except*`
        is a valid future extension and the walker must be robust to
        it before the first `except*` clause lands anywhere in main.py.
        """
        src = textwrap.dedent("""
            try:
                inner_call.mark_processed()
            except* Exception:
                pass
        """)
        tree = ast.parse(src)
        # tree.body is [TryStar] — pass it in as a top-level element.
        assert isinstance(tree.body[0], ast.TryStar), (
            f"fixture invariant: `try/except*` must parse to ast.TryStar; "
            f"got {type(tree.body[0]).__name__}"
        )
        lines = _find_calls_in(tree.body, "mark_processed")
        assert lines == [], (
            f"_find_calls_in must skip a top-level ast.TryStar the same "
            f"way it skips ast.Try; got lines={lines}."
        )

    def test_nested_try_star_as_child_is_not_descended(self):
        """PR #12 finding #1 (child-descent variant): a nested TryStar
        inside an outer Try must also be skipped. Mirrors the
        `test_nested_try_as_child_is_not_descended` guard on the Try
        side.
        """
        src = textwrap.dedent("""
            def outer():
                try:
                    ok_call()
                    try:
                        nested_call.mark_processed()
                    except* Exception:
                        pass
                except Exception:
                    pass
        """)
        tree = ast.parse(src)
        outer_fn = tree.body[0]
        outer_try = outer_fn.body[0]
        # Sanity: outer body's nested Try should actually be a TryStar.
        nested = outer_try.body[1]
        assert isinstance(nested, ast.TryStar), (
            f"fixture invariant: nested `try/except*` must parse as "
            f"ast.TryStar; got {type(nested).__name__}"
        )
        lines = _find_calls_in(outer_try.body, "mark_processed")
        assert lines == [], (
            f"_find_calls_in must skip a nested ast.TryStar during child "
            f"descent; got lines={lines}."
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

    # PR #12 finding #8: consolidate the three parallel structures
    # (_METHODS tuple, callable_probes dict, return_shape dict) into a
    # single _CONTRACT dict keyed by method name. Each contract entry
    # carries the callable probes + expected return-shape predicate.
    # `_METHODS = tuple(_CONTRACT)` preserves the seam-grep API without
    # duplicating the source of truth.
    #
    # Split-drift hazard (mirror image of finding #8): the pre-refactor
    # shape had three iterables on the same key set (_METHODS,
    # callable_probes, return_shape) that could silently disagree — a
    # method added to _METHODS but forgotten in callable_probes would
    # blow up with a KeyError, but the reverse (present in
    # callable_probes but not in _METHODS) was silent noise. One dict
    # eliminates both drift classes.
    #
    # PR #12 finding #9: extend the `send_with_labels` probe with the
    # full-shape call (labels + attachments) the real seam actually
    # invokes at src/apply/review.py:374. The kwarg-only variant lets
    # a signature drift on `labels` / `attachments` slip past the
    # stripped-down probe.
    _CONTRACT: dict = {
        "search": {
            "probes": (
                lambda stub: stub.search("q"),
                lambda stub: stub.search("q", max_results=5),
            ),
            "return_shape": lambda r: r == [],
        },
        "get_or_create_label": {
            "probes": (
                lambda stub: stub.get_or_create_label("L"),
            ),
            "return_shape": lambda r: r == "stub:L",
        },
        "send_with_labels": {
            "probes": (
                # Minimal-shape probe (kw-only subject/body/to only).
                lambda stub: stub.send_with_labels(
                    subject="s", body="b", to="t"
                ),
                # Real-seam-shape probe: labels + attachments kwargs the
                # seam passes at src/apply/review.py:374. Any signature
                # drift on labels or attachments surfaces here.
                lambda stub: stub.send_with_labels(
                    subject="s",
                    body="b",
                    to="t",
                    labels=["Lbl"],
                    attachments=["/tmp/x"],
                ),
            ),
            "return_shape": lambda r: isinstance(r, tuple) and len(r) == 2,
        },
        "apply_label": {
            "probes": (
                lambda stub: stub.apply_label("m", "L"),
            ),
            "return_shape": lambda r: r is None,
        },
        "remove_label": {
            "probes": (
                lambda stub: stub.remove_label("m", "L"),
            ),
            "return_shape": lambda r: r is None,
        },
        "reply_to_thread": {
            "probes": (
                lambda stub: stub.reply_to_thread("t", "b"),
            ),
            "return_shape": lambda r: isinstance(r, str),
        },
    }
    _METHODS = tuple(_CONTRACT)

    def test_seam_gmail_methods_all_covered_by_METHODS(self):
        """iter-3 (finding #3): if the seam adds a new `gmail.<method>()`
        call and _METHODS doesn't stub it, --test mode crashes but the
        signature/callable tests both stay green (they only iterate what
        _METHODS already lists — the mirror image of the M17 drift bug).

        Discover every `gmail.<method>(...)` call site in the SEAM
        (src/apply/*) — the code path that receives _NoopGmailClient in
        --test mode. (src/main.py invokes methods on the REAL GmailClient
        directly in the production branch, not on the stub, so those are
        out of scope for the stub's contract.)

        PR #12 findings #3 + #4: AST-based discovery replaces the
        line-regex `\\bgmail\\.<attr>\\(` walk.

        The line-regex has three concrete false-positive / false-negative
        classes the AST walk eliminates:
          - False negatives on multi-line calls: `gmail.<method>(\\n  ...)`
            fits the regex on the first line, but when the identifier
            spans a comment or newline (e.g. wrapped call at 80 cols),
            the pattern misses.
          - False positives on attribute chains + docstrings: an
            attribute reference like `something.gmail.send_with_labels`
            in a docstring or comment is matched by `\\bgmail\\.<attr>\\(`,
            adding a phantom method the stub does NOT need to cover.
          - New seam modules (added to src/apply/) bypass coverage
            entirely because `seam_files` was a fixed two-file list.

        AST walk: parse every `src/apply/*.py`, iterate `ast.walk`, and
        collect method names for `ast.Call` whose func is an `ast.Attribute`
        on `ast.Name(id='gmail')` — the only syntactic shape that binds
        to the injected `_NoopGmailClient` in --test mode. Comments,
        docstrings, and attribute chains that do NOT root at `gmail`
        are structurally excluded.
        """
        # PR #12 finding #4: discover files instead of hard-coding a list.
        # A new seam module added under src/apply/ automatically enters
        # coverage; the old fixed list would silently skip it.
        seam_files = sorted((ROOT / "src" / "apply").glob("*.py"))
        assert seam_files, (
            "src/apply/*.py discovered zero files — the seam directory "
            "was renamed. Update the glob in this test."
        )

        called = set()
        for path in seam_files:
            source = path.read_text()
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:  # pragma: no cover — parse errors bubble
                raise AssertionError(
                    f"seam file {path} failed to parse: {exc}"
                ) from exc
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                # Root the attribute chain at `gmail` — this is the seam's
                # binding name for the injected client. `gmail.foo(...)`
                # matches; `something.gmail.foo(...)` does NOT (the outer
                # Attribute's `value` is another Attribute, not a Name).
                if not isinstance(func.value, ast.Name):
                    continue
                if func.value.id != "gmail":
                    continue
                called.add(func.attr)

        assert called, (
            "Seam AST walk returned zero gmail.<method>() calls — the "
            "seam files were probably renamed. Check the src/apply/*.py "
            "glob."
        )

        missing = called - set(self._METHODS)
        assert not missing, (
            f"Seam invokes gmail methods NOT covered by _NoopGmailClient "
            f"stub / _METHODS: {sorted(missing)}. --test mode will crash "
            f"AttributeError the first time these run. Add them to "
            f"_NoopGmailClient + _METHODS."
        )

        # PR #12 finding #5: reverse-direction stale-methods check is
        # code-hygiene noise (the docstring itself acknowledged as much),
        # not a runtime bug. A hard `assert not stale` promoted noise
        # to a test failure that would block PRs on unrelated cleanups.
        # Downgraded to a warning — the code-hygiene signal survives
        # without blocking otherwise-clean test runs.
        stale = set(self._METHODS) - called
        if stale:
            import warnings
            warnings.warn(
                f"_METHODS lists methods no longer called by the seam: "
                f"{sorted(stale)}. Consider removing them from _METHODS "
                f"+ _NoopGmailClient. (Downgraded from hard assert per "
                f"PR #12 finding #5 — code-hygiene, not runtime bug.)",
                stacklevel=2,
            )

    def test_noop_gmail_client_matches_real_and_is_callable(self):
        """Combined signature + callability contract. Consolidated from two
        tests in iter-2 (finding #8) — the split iterated the same _METHODS
        tuple twice, and could silently disagree about the contract.

        PR #12 finding #8: the parallel `callable_probes` + `return_shape`
        dicts are folded into `_CONTRACT` (one dict on `self`). The
        split-drift class (add a method to the tuple but forget one of
        the two dicts) is structurally impossible now — each entry
        carries its own probes and return-shape predicate as one unit.

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

        for name, contract in self._CONTRACT.items():
            assert hasattr(stub_cls, name), (
                f"_NoopGmailClient missing method {name!r}"
            )
            assert hasattr(real_cls, name), (
                f"GmailClient missing method {name!r} — test is stale?"
            )

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
            probes = contract["probes"]
            assert probes, (
                f"PR #12 finding #8: _CONTRACT[{name!r}] missing probes"
            )
            return_shape = contract["return_shape"]
            for probe in probes:
                result = probe(stub)
                assert return_shape(result), (
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
# Phase 5 iter-3 CRITICAL #5 — M7-class altitude fix at parser extraction.
#
# The parser is the sole choke point through which the empty-string sentinel
# entered the pipeline (em-/en-dash split with a leading dash on the raw div
# text produces `parts[0].strip() == ""`). Every downstream consumer of
# `job['company']` — renderer filename, cover-letter LLM prompt, HM-finder
# LLM prompt, apply/review notify subject line, apply/notify body line, and
# the auto-apply dedup index — reads its value from this choke point.
#
# Fixing at parser extraction (`parts[0].strip() or "Unknown"`) eliminates
# the empty-string cascade class at a single site rather than patching each
# downstream consumer independently. Two review-hardened refinements:
#   1. `_strip_format_chars(raw)` catches Unicode Cf (format) chars — ZWSP,
#      BOM, LRM/RLM, ZWJ/ZWNJ — which `str.strip()` alone does NOT touch and
#      which would otherwise leak an invisible-but-truthy sentinel past the
#      `.strip() or "Unknown"` fallback (M7-class silent drop reintroduced).
#   2. `location = parts[1].strip() or None` sweeps the same-class sibling
#      on the location side — a trailing-dash raw ("Acme —") would otherwise
#      set `location=""`, which `.get('location', 'Not specified')` does NOT
#      catch downstream.
#
# Tests below prove: (a) no empty/invisible-shaped value ever leaves the
# parser regardless of raw div shape, and (b) downstream sites read the
# safely-normalized value without any downstream code change.
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmailParserCompanyNormalizationAtExtraction:
    """Altitude fix behavioral regression guard (Phase 5 iter-3 CRITICAL #5).

    Class naming follows the existing `TestEmailParser*` prefix used by the
    other parser test classes in this file (`TestEmailParserGoldenFromSampleAlert`,
    `TestEmailParserDedupByTitleAndCompany`).
    """

    def test_em_dash_leading_normalizes_company_to_unknown(self):
        """Primary altitude assertion: parser must not return empty string.

        RED on pre-fix branch state: `parts[0].strip()` for raw='— Remote'
        returns '' (empty string). The (title, url) dedup-fallback (iter-2)
        surfaces both distinct-URL jobs — with `company=''` silently
        corrupting downstream filename/prompt/subject-line renderings.

        GREEN after altitude fix: `parts[0].strip() or "Unknown"` normalizes
        the empty result to the "Unknown" sentinel, matching the default
        fallback the parser already sets when the company div is missing.
        """
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>— Remote</div>"  # em-dash split → parts[0].strip()==''
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
            f"iter-2 URL-fallback dedup must still surface both distinct-URL "
            f"jobs; got {len(jobs)}"
        )
        for j in jobs:
            assert j["company"] == "Unknown", (
                f"em-dash empty-string case must normalize to 'Unknown' "
                f"sentinel. Got {j['company']!r}."
            )

    def test_en_dash_leading_normalizes_company_to_unknown(self):
        """Sibling of em-dash test — en-dash split path. Same class of
        bug: `raw='– Remote'` → split gives `parts[0]=''` → `.strip()==''`.
        """
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Content Manager</span></h3>"
            "<div>– Remote</div>"  # en-dash split → parts[0].strip()==''
            '<a href="https://example.com/en1">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Unknown"
        assert jobs[0]["location"] == "Remote"

    def test_zero_width_space_prefixed_raw_does_not_bypass_fallback(self):
        """Iter-1 pessimist proof-of-bug: `str.strip()` does NOT remove
        Unicode Cf (format) chars — ZERO WIDTH SPACE U+200B, BOM U+FEFF,
        LRM/RLM, ZWJ/ZWNJ. `isspace()` returns False for all of them.

        Pre-hardening: raw='\\u200b— Remote' → parts[0].strip() = '\\u200b'
        (length 1, truthy). The `or "Unknown"` fallback is bypassed and
        `company='\\u200b'` — an invisible-but-truthy sentinel — leaks
        downstream. `_safe_filename('\\u200b')` returns '' (word-char
        strip drops it), reintroducing the exact filename-collision
        cascade the altitude fix targets.

        Post-hardening: `_strip_format_chars(raw)` removes Cf chars once
        at the top of the extraction block, so every downstream branch
        (em-dash split, en-dash split, `elif raw:`) inherits the invariant
        that raw carries no invisibles by the time the `.strip() or "Unknown"`
        fallback runs.

        Mutation check: remove the `_strip_format_chars(...)` wrapper from
        the `raw = ...` line in email_parser.py. This test fails: parser
        returns `company='\\u200b'` and the downstream filename becomes
        the same underscore-leading collision as the empty-string case.
        """
        from parser.email_parser import parse_alert_email
        from pdf_gen.renderer import _safe_filename
        html = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            # ZWSP (U+200B) prefix — invisible in a rendered email, present
            # in the DOM raw text. Real hiring.cafe alerts occasionally
            # include ZWSPs from copy-pasted email templates.
            "<div>​— Remote</div>"
            '<a href="https://example.com/zwsp1">Apply</a>'
            "</td>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>​— Remote</div>"
            '<a href="https://example.com/zwsp2">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        # Both jobs must surface — if ZWSP leaked past the fallback,
        # `no_real_company = (company == "Unknown") or (not company.strip())`
        # would evaluate False (both) and dedup would key on
        # (title, '​') for BOTH cards → silent M7-class drop.
        assert len(jobs) == 2, (
            f"ZWSP-prefixed cards must dedup via the (title, url) fallback "
            f"— parser leaked the ZWSP sentinel and reintroduced M7 silent "
            f"drop. Got {len(jobs)} jobs: "
            f"{[(j['title'], repr(j['company'])) for j in jobs]}"
        )
        for j in jobs:
            assert j["company"] == "Unknown", (
                f"parser leaked ZWSP (U+200B) sentinel for job {j!r}. "
                f"`_strip_format_chars` at extraction top must remove all "
                f"Cf-category chars before the `.strip() or 'Unknown'` "
                f"fallback runs."
            )
            # Also verify the downstream _safe_filename check: the ZWSP
            # bypass produced `_safe_filename('​') == ''` in the
            # pessimist proof-of-bug. After the fix, the filename must be
            # a well-formed 'Unknown_...' prefix.
            fname_base = _safe_filename(j["company"])
            # PR #12 finding #12: `_safe_filename` now appends a short
            # hash of the input for ZWNJ-collision guarding. The
            # human-readable prefix is what matters for the M7 altitude
            # invariant this test asserts; the hash tail is the
            # per-input discriminator.
            assert fname_base.startswith("Unknown_"), (
                f"downstream renderer filename must receive a well-formed "
                f"company component after altitude fix + Cf sweep; got "
                f"_safe_filename({j['company']!r}) = {fname_base!r}."
            )

    def test_hostile_cf_and_cc_chars_all_stripped_at_extraction(self):
        """Sweep across the denylist-inverted invisible strip
        (`_strip_format_chars`). Under the iter-3 policy every Cf char
        EXCEPT ZWJ/ZWNJ is stripped, so this matrix covers:

        Category Cf — attack-class chars historically used to smuggle
        invisible sentinels:
          - ZWSP  (U+200B) — zero-width space
          - LRM   (U+200E) / RLM (U+200F) — direction marks
          - WJ    (U+2060) — word joiner
          - BOM   (U+FEFF) — zero-width no-break space
          - RLO   (U+202E) — right-to-left override (bidi attack surface)
          - LRO   (U+202A) — left-to-right embed
          - PDI   (U+2069) — pop directional isolate
          - RLI   (U+2067) — right-to-left isolate
          - SHY   (U+00AD) — soft hyphen (may render as `-` in some contexts)
          - ALM   (U+061C) — arabic letter mark
          - MVS   (U+180E) — mongolian vowel separator

        Category Cc — non-isspace controls that survive `str.strip()`:
          - BEL   (U+0007) — a Cc char that survives lxml roundtrip
                            (NUL is normalized by lxml so we can't
                            exercise it through the parser)

        Any of these — including a codepoint added to Unicode Cf AFTER
        this code shipped — could otherwise smuggle a truthy invisible
        past `.strip()` and reintroduce the M7 silent-drop cascade.
        Iter-2 shipped a curated allowlist that missed 12 Cf chars in
        this class (bidi override/isolate, SHY, ALM, MVS); the iter-3
        denylist inversion eliminates the "did we remember this one?"
        maintenance risk.
        """
        from parser.email_parser import parse_alert_email
        hostile = [
            "​",  # ZWSP
            "‎",  # LRM
            "‏",  # RLM
            "⁠",  # WORD JOINER
            "﻿",  # BOM
            "‮",  # RLO — bidi override (iter-3 security repro)
            "‪",  # LRO
            "⁩",  # PDI — bidi isolate
            "⁧",  # RLI
            "­",  # SOFT HYPHEN (iter-3 correctness repro)
            "؜",  # ARABIC LETTER MARK
            "᠎",  # MONGOLIAN VOWEL SEPARATOR
            "\x07",    # BEL — Cc non-isspace; NUL is eaten by lxml so
                       #  BEL exercises the Cc branch through the DOM
        ]
        card_html = "".join(
            f"<td><h3><span>Role {i}</span></h3>"
            f"<div>{ch}— Remote</div>"
            f'<a href="https://example.com/cf{i}">Apply</a>'
            "</td>"
            for i, ch in enumerate(hostile)
        )
        html = f"<html><body><table>{card_html}</table></body></html>"
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == len(hostile), (
            f"expected {len(hostile)} distinct-title cards to surface, "
            f"got {len(jobs)}: "
            f"{[(j['title'], repr(j['company'])) for j in jobs]}"
        )
        for j in jobs:
            assert j["company"] == "Unknown", (
                f"parser leaked a hostile invisible: {j!r}. "
                f"`_strip_format_chars` must strip ALL Cf except "
                f"ZWJ/ZWNJ. If this fails on a specific card, the "
                f"invisible codepoint slipped the denylist."
            )

    def test_zwj_only_prefix_does_not_bypass_fallback(self):
        """Iter-4 correctness reviewer surfaced residual M7 silent-drop
        attack on the two Cf chars we PRESERVE (ZWJ U+200D, ZWNJ U+200C).

        A raw div like `"‍— Remote"` — ZWJ-prefixed — leaves
        `parts[0].strip() = "‍"` (length 1, truthy, non-whitespace).
        `_strip_format_chars` correctly preserves ZWJ for legitimate
        script/emoji use, so the sweep does NOT remove it. The `.strip()
        or "Unknown"` fallback saw a truthy value and passed it through.
        The `no_real_company` guard's `not company.strip()` disjunct
        evaluates False. Two same-title distinct-URL cards dedup to
        `(title, "‍")` → silent M7 drop.

        Fix: `_has_meaningful_content` rejects strings composed only of
        Cf/whitespace chars, so ZWJ-only OR ZWNJ-only prefixes fall
        through to the "Unknown" fallback the same as empty results.
        Meaningful content anywhere in the string (mid-name ZWJ in a real
        Persian company name) still passes through untouched.
        """
        from parser.email_parser import parse_alert_email
        # ZWJ-prefix pair
        html_zwj = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>‍— Remote</div>"  # ZWJ prefix
            '<a href="https://example.com/zwj1">Apply</a>'
            "</td>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            "<div>‍— Remote</div>"
            '<a href="https://example.com/zwj2">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html_zwj, max_jobs=99)
        assert len(jobs) == 2, (
            f"ZWJ-prefixed cards must dedup via (title, url) fallback; "
            f"got {len(jobs)} jobs: "
            f"{[(j['title'], repr(j['company'])) for j in jobs]}"
        )
        for j in jobs:
            assert j["company"] == "Unknown", (
                f"parser leaked a ZWJ-only company sentinel: {j!r}. "
                f"`_has_meaningful_content` must reject Cf-preserved-only "
                f"strings."
            )
        # ZWNJ-prefix pair — same attack, different preserved char.
        html_zwnj = (
            "<html><body><table>"
            "<td><h3><span>Content Manager</span></h3>"
            "<div>‌— Remote</div>"  # ZWNJ prefix
            '<a href="https://example.com/zwnj1">Apply</a>'
            "</td>"
            "<td><h3><span>Content Manager</span></h3>"
            "<div>‌— Remote</div>"
            '<a href="https://example.com/zwnj2">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html_zwnj, max_jobs=99)
        assert len(jobs) == 2
        for j in jobs:
            assert j["company"] == "Unknown", (
                f"parser leaked a ZWNJ-only company sentinel: {j!r}"
            )

    def test_short_title_of_only_zwnj_is_rejected(self):
        """Iter-4 correctness warning: `title` participates in the
        `(title, company)` dedup key, so the same invisible-sentinel
        hardening the company field carries.

        Pre-fix: a title of three ZWNJ chars ('\\u200c\\u200c\\u200c') has
        `len(title) == 3` so passes the `len < 3` guard, then flows
        through to the LLM cover-letter prompt as `Title: ‌‌‌`. Same
        cascade class as company. Title is now swept + meaningful-content
        checked at the same choke point.
        """
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>‌‌‌</span></h3>"
            "<div>Acme — Remote</div>"
            '<a href="https://example.com/badtitle">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 0, (
            f"parser accepted a Cf-only title of length 3: {jobs!r}. "
            f"`_has_meaningful_content` must reject titles composed only "
            f"of ZWJ/ZWNJ/whitespace."
        )

    def test_zwj_and_zwnj_preserved_in_legitimate_company_names(self):
        """Iter-2 security review: `_strip_format_chars` MUST preserve
        ZWJ (U+200D) and ZWNJ (U+200C). Both are semantically load-bearing:
          - ZWNJ is required for Persian/Urdu word segmentation
          - ZWJ controls Devanagari/Bengali/Tamil conjunct rendering AND
            is the glue in every multi-codepoint emoji sequence
        A blanket Cf strip would silently corrupt legitimate non-English
        company names.

        Mutation check: change `_strip_format_chars` to strip the entire
        Cf category. This test fails: the Persian company name loses its
        ZWNJ boundaries and the emoji sequence loses its ZWJ glue.
        """
        from parser.email_parser import parse_alert_email
        # A Persian company name using ZWNJ as a word boundary. If the
        # ZWNJ is stripped, the two morphemes concatenate and the name
        # is silently corrupted.
        persian_name = "دیجی‌کالا"  # Digikala, ZWNJ between morphemes
        # A ZWJ-glued emoji sequence — family emoji is a canonical case.
        # If the ZWJ is stripped, the sequence decomposes into individual
        # emoji (silent corruption of a company logo).
        emoji_name = "Foo‍Corp"  # arbitrary ZWJ-preservation case
        html = (
            "<html><body><table>"
            f"<td><h3><span>Role Persian</span></h3>"
            f"<div>{persian_name} — Tehran</div>"
            f'<a href="https://example.com/fa">Apply</a>'
            "</td>"
            f"<td><h3><span>Role Zwj</span></h3>"
            f"<div>{emoji_name} — Remote</div>"
            f'<a href="https://example.com/zwj">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 2
        by_title = {j["title"]: j for j in jobs}
        assert by_title["Role Persian"]["company"] == persian_name, (
            f"ZWNJ stripped from Persian company name — "
            f"`_CF_PRESERVE` (the denylist-inversion carve-out in "
            f"`_strip_format_chars`) must include U+200C. "
            f"Got {by_title['Role Persian']['company']!r}."
        )
        assert by_title["Role Zwj"]["company"] == emoji_name, (
            f"ZWJ stripped from company name — `_CF_PRESERVE` must "
            f"include U+200D. "
            f"Got {by_title['Role Zwj']['company']!r}."
        )

    def test_elif_raw_branch_normalizes_cf_only_input(self):
        """Third extraction site (`elif raw:` — no dash present).

        Pre-hardening: raw was assigned directly to `company` after just
        the `elif raw:` truthy check. A raw like '\\u200b' (single ZWSP)
        was truthy AND had no dash, so `company='\\u200b'` leaked.

        Post-hardening: `_strip_format_chars` removes the ZWSP before the
        `elif raw:` check even runs, and the fallback branch checks
        `elif raw.strip():` so pure-whitespace remnants (e.g. Cf sweep
        left ` `) also fall through to the default `Unknown`.
        """
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Role Cf Only</span></h3>"
            "<div>​</div>"  # ZWSP only, no dash, no real content
            '<a href="https://example.com/cfonly">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Unknown", (
            f"`elif raw:` branch leaked a ZWSP-only company: "
            f"{jobs[0]['company']!r}"
        )

    def test_trailing_dash_leaves_empty_location_as_empty_string(self):
        """Parser side of the PR #12 finding-#11 sweep.

        Iter-1 arch + correctness reviewers flagged the `parts[1].strip()`
        empty-string case as a same-class sibling of the company sentinel.
        Iter-2 correctness reviewer countered that coercing parser output
        to `None` REGRESSES downstream rendering: `{'location': None}.get(
        'location', 'Not specified')` returns `None`, causing the LLM
        prompt to render literal `"Location: None"`.

        PR #12 resolution: parser continues to return `""` (unchanged),
        and the three downstream consumers (`cover_letter.py`,
        `hm_finder.py`, `digest.py`) now use `.get('location') or default`
        — which coerces both `""` and `None` to the human-readable
        fallback. Coverage of the downstream contract lives in
        `TestLocationEmptyStringDownstream` below.

        This test locks the parser-side invariant: no silent shape flip
        (e.g. someone coerces empty to `None` in `_split_company_location`)
        can regress the downstream contract.
        """
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Trailing Dash</span></h3>"
            "<div>Acme —</div>"  # em-dash trailing → parts[1].strip()==''
            '<a href="https://example.com/td">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Acme"
        # Parser contract locked at `""` (empty string). Downstream sites
        # own the coercion to a human-readable default via `.get(...) or`.
        assert jobs[0]["location"] == "", (
            f"parser location contract: trailing-dash raw yields empty "
            f"string (downstream consumers own the fallback). Got "
            f"{jobs[0]['location']!r}."
        )


class TestEmailParserCompanyNormalizationDownstream:
    """Altitude proof — downstream sites receive safely-shaped company
    values WITHOUT any downstream code change. Each test exercises the
    real downstream template/function against parser output.

    Downstream consumers verified:
      - src/pdf_gen/renderer.py:_safe_filename (renderer filenames)
      - src/tailor/cover_letter.py:79-83 (LLM prompt Company: line)

    Not exercised here (reasonable extension for a follow-up sweep):
      - src/contacts/hm_finder.py LLM prompt Company: line
      - src/apply/review.py notify subject line
      - src/apply/notify.py notify body company: line
    All three read the same `company` field via the same shape, so the
    parser altitude fix inoculates them too.
    """

    def _job_from_html(self, div_content: str) -> dict:
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Product Marketing Manager</span></h3>"
            f"<div>{div_content}</div>"
            '<a href="https://example.com/dw">Apply</a>'
            "</td></table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1
        return jobs[0]

    def test_renderer_filename_gets_non_empty_company_prefix(self):
        """`_safe_filename(company)` must return a non-empty, well-formed
        string for every extraction path — otherwise the resume filename
        `{company}_{title}_Resume.docx` collides on same-title jobs.
        """
        from pdf_gen.renderer import _safe_filename
        for div in ("— Remote", "– Remote", "​— Remote", "﻿– Remote"):
            job = self._job_from_html(div)
            fname_prefix = _safe_filename(job["company"])
            # PR #12 finding #12: _safe_filename appends a `_XXXX` hex
            # discriminator. The human-readable prefix (what a user
            # sees on disk) is the "Unknown" sentinel — that is the
            # altitude invariant this test guards.
            assert fname_prefix.startswith("Unknown_"), (
                f"div={div!r}: renderer filename received unsafe company "
                f"prefix {fname_prefix!r} (expected 'Unknown_<hex>')."
            )

    def test_cover_letter_llm_prompt_gets_named_company(self):
        """The cover-letter LLM prompt template (src/tailor/cover_letter.py
        lines 79-83) is:
            Title: {job['title']}
            Company: {job['company']}
            Location: ...

        Pre-fix + pre-hardening: `Company: ` (empty) or `Company: \\u200b`
        (invisible). Both malform the prompt — the LLM sees a Company:
        field with no value or an invisible-only value and generates
        arbitrary output that may reference a hallucinated company name.

        Correctness reviewer flagged that testing the line-118 fallback
        text is misleading because `_clean_text` scrubs the double-space
        artifact in production. The LLM PROMPT (line 83) is where the
        actual harm lands — unscrubbed, un-clean_text'd, sent directly
        to the model.
        """
        for div in ("— Remote", "– Remote", "​— Remote"):
            job = self._job_from_html(div)
            # Same template as src/tailor/cover_letter.py:79-84 prompt.
            prompt_snippet = (
                f"Title: {job['title']}\n"
                f"Company: {job['company']}\n"
                f"Location: {job.get('location', 'Not specified')}"
            )
            assert "Company: Unknown" in prompt_snippet, (
                f"div={div!r}: LLM prompt Company: line malformed: "
                f"{prompt_snippet!r}"
            )
            # Neither an empty tail (`Company: \n`) nor an invisible-only
            # tail (`Company: ​\n`) should appear.
            assert "Company: \n" not in prompt_snippet
            assert "Company: ​" not in prompt_snippet


class TestEmailParserCompanyNormalizationBehavioralGuard:
    """Behavioral mutation guard — asserts the invariant that no
    empty/whitespace/Cf-only company value ever leaves the parser,
    regardless of raw div shape.

    (Superseded the earlier source-grep guard test which the iter-1
    reviewers flagged as brittle to legitimate refactors — helper
    extraction, quote-style reflow, multi-line wrap. The behavioral
    sweep here catches the same mutations via observable output.)
    """

    def test_parser_never_returns_empty_or_invisible_company(self):
        """Property sweep across every extraction path that could
        previously leak an unsafe-shaped company value.

        Mutation matrix:
          - Drop `_strip_format_chars` at extraction top → ZWSP/BOM/LRM
            cards leak Cf-shaped company.
          - Drop `or "Unknown"` from em-dash branch → em-dash cards leak
            empty company.
          - Drop `or "Unknown"` from en-dash branch → en-dash cards leak
            empty company.
          - Drop the `elif raw.strip():` guard → all-whitespace remnants
            after Cf sweep leak whitespace-only company.
        Each mutation surfaces here via the invariant assertion.
        """
        import unicodedata
        from parser.email_parser import parse_alert_email
        # Distinct titles to avoid dedup fallback; each card exercises a
        # different extraction path that historically could leak.
        cards = [
            ("Role Em", "— Remote"),
            ("Role En", "– Remote"),
            ("Role EmNbsp", "\xa0—\xa0Remote"),
            ("Role EnNbsp", "\xa0–\xa0Remote"),
            ("Role EmZwsp", "​— Remote"),
            ("Role EnZwsp", "​– Remote"),
            ("Role Bom", "﻿— Remote"),
            ("Role Rlo", "‮— Remote"),   # bidi override (iter-3)
            ("Role Shy", "­— Remote"),   # soft hyphen (iter-3)
            ("Role Alm", "؜— Remote"),   # arabic letter mark
            ("Role Bel", "\x07— Remote"),     # Cc, not isspace, lxml-safe
            ("Role CfOnlyElif", "​"),    # elif raw: branch
        ]
        card_html = "".join(
            f"<td><h3><span>{title}</span></h3>"
            f"<div>{div}</div>"
            f'<a href="https://example.com/{i}">Apply</a>'
            "</td>"
            for i, (title, div) in enumerate(cards)
        )
        html = f"<html><body><table>{card_html}</table></body></html>"
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == len(cards), (
            f"expected {len(cards)} distinct-title jobs, got {len(jobs)}"
        )
        for j in jobs:
            c = j["company"]
            # Invariant: no empty, no whitespace-only, no invisible-only
            # value ever leaves the parser. Empty-string check is
            # explicit before the `all` check to avoid the empty-iterable
            # false-positive (all([]) == True).
            assert c, f"parser leaked empty company for job {j!r}"
            assert c.strip(), (
                f"parser leaked whitespace-only company for job {j!r}"
            )
            assert not all(
                unicodedata.category(ch) in ("Cf", "Cc") for ch in c
            ), f"parser leaked invisible-only company for job {j!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# PR #12 finding #11 — trailing-dash location downstream sweep.
#
# Parser leaves `location=""` on trailing-dash raws (`"Acme —"`) as an
# intentional scope-out (see the PR #11 altitude-fix rationale: coercing to
# `None` regresses `.get('location', 'Not specified')` — that pattern
# returns `None` when the key exists with value None, rendering literal
# `"Location: None"` in downstream LLM prompts and digest lines).
#
# PR #12 sweeps the three downstream sites — `contacts/hm_finder.py`,
# `tailor/cover_letter.py`, `gmail/digest.py` — to `.get('location') or
# default` so both empty-string and None values coerce to the human-
# readable fallback. Tests below exercise the real template/f-string at
# each site against parser output for a trailing-dash raw.
# ═══════════════════════════════════════════════════════════════════════════════


class TestLocationEmptyStringDownstream:
    """PR #12 finding #11 — sweep-for-same-class-instances codified rule.

    The parser's `_split_company_location` correctly returns `location=""`
    for trailing-dash raws (see docstring on `_split_company_location`).
    Three downstream sites read `job['location']` via
    `.get('location', default)`, which returns `""` — NOT the default —
    when the key exists with an empty value. This mirrors the company-
    side sentinel PR #11 fixed at the parser choke point.

    Post-sweep contract: every downstream site uses `.get(...) or default`
    so both `""` and `None` fall through to the human-readable fallback.

    Sites covered:
      - `src/contacts/hm_finder.py:102`  — LLM prompt `Location:` line
      - `src/tailor/cover_letter.py:84`  — LLM prompt `Location:` line
      - `src/gmail/digest.py:123`        — digest text `({location})` tail
    """

    def _trailing_dash_job(self) -> dict:
        """Parse a synthetic card whose company div is a trailing-dash
        raw (`"Acme —"`). Parser returns `location=""` for this shape."""
        from parser.email_parser import parse_alert_email
        html = (
            "<html><body><table>"
            "<td><h3><span>Trailing Dash Role</span></h3>"
            "<div>Acme —</div>"  # parts[1].strip() == ""
            '<a href="https://example.com/td">Apply</a>'
            "</td>"
            "</table></body></html>"
        )
        jobs = parse_alert_email(html_body=html, max_jobs=99)
        assert len(jobs) == 1
        assert jobs[0]["location"] == "", (
            f"fixture invariant: parser must yield empty-string location "
            f"for trailing-dash raw. Got {jobs[0]['location']!r}."
        )
        return jobs[0]

    def test_hm_finder_prompt_renders_fallback_for_empty_location(self):
        """`src/contacts/hm_finder.py:97-104` builds an LLM prompt with
        `Location: {job.get('location') or 'Not specified'}`. Pre-sweep,
        an empty-string location renders `Location: ` (blank tail) which
        malforms the prompt — the LLM sees a `Location:` label with no
        value and is free to hallucinate one.

        Post-sweep: `.get('location') or 'Not specified'` coerces `""`
        (and `None`) to the fallback. This test exercises `_build_prompt`
        directly with parser output for a trailing-dash raw.

        Mutation check: revert to `.get('location', 'Not specified')` —
        the assertion below fails because `.get()` returns `""`, not the
        default, when the key exists with an empty value.
        """
        from contacts.hm_finder import _build_prompt
        job = self._trailing_dash_job()
        prompt = _build_prompt(
            job=job,
            jd_text="stub job description",
            lane="marketing",
            clues={"reports_to": None, "hm_name": None},
            hm_titles=["VP Marketing", "Director Marketing"],
            config={"generate_outreach_note": False},
        )
        assert "Location: Not specified" in prompt, (
            f"hm_finder LLM prompt must coerce empty-string location to "
            f"'Not specified'. Prompt snippet:\n{prompt[:500]!r}"
        )
        # Blank-tail regression: `Location: \n` in the built prompt would
        # indicate the .get() call returned '' instead of the fallback.
        assert "Location: \n" not in prompt, (
            f"hm_finder LLM prompt leaked a blank-tail Location line — "
            f"`.get('location', default)` returned '' instead of the "
            f"fallback. Prompt snippet:\n{prompt[:500]!r}"
        )

        # Also source-grep the fix to catch a future refactor that
        # replaces the built-prompt line without touching this test.
        source = (ROOT / "src" / "contacts" / "hm_finder.py").read_text()
        assert "job.get('location') or 'Not specified'" in source, (
            f"src/contacts/hm_finder.py must use `.get('location') or "
            f"'Not specified'` — the `.get('location', 'Not specified')` "
            f"form returns '' when the key exists with an empty value."
        )

    def test_cover_letter_prompt_renders_fallback_for_empty_location(self):
        """`src/tailor/cover_letter.py:79-85` builds an LLM prompt with
        `Location: {job.get('location', 'Not specified')}`. Same class
        as the hm_finder test above — exercise the real prompt string
        against parser output.

        The cover-letter prompt is constructed via an f-string inside
        `tailor_cover_letter`. Rather than invoke the full function
        (which requires a project bank + LLM call), we assert the
        f-string template shape directly via source-grep to prove the
        `.get(...) or default` pattern is in place. A behavioral
        assertion via a synthetic f-string mirrors the prompt at the
        actual line.
        """
        # Behavioral: build the same f-string the prompt uses.
        job = self._trailing_dash_job()
        prompt_snippet = (
            f"Title: {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Location: {job.get('location') or 'Not specified'}\n"
        )
        assert "Location: Not specified" in prompt_snippet, (
            f"cover-letter prompt shape must coerce empty-string "
            f"location to 'Not specified': {prompt_snippet!r}"
        )
        assert "Location: \n" not in prompt_snippet

        # Source-grep: prove the fix is actually in place at line 84,
        # not just in the test's mirrored f-string.
        source = (ROOT / "src" / "tailor" / "cover_letter.py").read_text()
        assert "job.get('location') or 'Not specified'" in source, (
            f"src/tailor/cover_letter.py must use `.get('location') or "
            f"'Not specified'` — the `.get('location', 'Not specified')` "
            f"form returns '' when the key exists with an empty value, "
            f"reintroducing the blank-tail regression."
        )

    def test_digest_renders_fallback_for_empty_location(self):
        """`src/gmail/digest.py:123` composes the processed-job digest
        line with `location = job.get("location", "Unknown")`. Same
        class — pre-sweep renders `({job['location']})` as `()` (empty
        parens) instead of `(Unknown)`.
        """
        from gmail.digest import compose_digest
        job = self._trailing_dash_job()
        # Add renderer-flavor fields compose_digest expects on processed jobs.
        job.update({
            "url": "https://example.com/td",
            "lane": "marketing",
        })
        digest = compose_digest(
            processed=[job],
            skipped=[],
            attachments=[],
        )
        assert "(Unknown)" in digest, (
            f"digest must coerce empty-string location to '(Unknown)'. "
            f"Digest snippet:\n{digest!r}"
        )
        # No blank-parens tail (`()`) should reach the digest.
        assert " ()" not in digest, (
            f"digest leaked a blank `()` location — `.get('location', "
            f"default)` returned '' instead of the fallback."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PR #12 finding #12 — _safe_filename ZWNJ collision guard.
#
# `_safe_filename` uses `re.sub(r"[^\w\s-]", "", text)` which strips ZWJ +
# ZWNJ because Python's `\w` excludes the Cf category. Two Persian company
# names that differ only in mid-name ZWNJ position collapse to identical
# sanitized shapes; when both jobs share a title, the renderer produces
# `{company}_{title}_Resume.docx` on the same output path and the second
# render silently overwrites the first.
#
# Fix: append a short hash of the ORIGINAL input text so filenames stay
# distinct even after the regex strip erases the discriminating chars.
# ═══════════════════════════════════════════════════════════════════════════════


class TestSafeFilenameZwnjCollisionGuard:
    """PR #12 finding #12 — mid-name ZWNJ filename collision.

    Property: two inputs that differ ONLY in a Cf codepoint the regex
    strip erases (ZWNJ/ZWJ) MUST produce distinct `_safe_filename`
    outputs. Otherwise the renderer's dual filename (`Company_Title_
    Resume.docx`) collides on the same disk path for two same-title
    Persian-company jobs, silently overwriting the first render.

    Mutation check: revert `_safe_filename` to the pre-fix body (drop
    the hash suffix). Both assertions below fail.
    """

    def test_persian_zwnj_position_produces_distinct_filenames(self):
        """Two Persian company names differ only in mid-name ZWNJ
        position. Post-Cf-strip they collapse to the same string, so
        the fix must inject a distinguishing tail derived from the
        original input.
        """
        from pdf_gen.renderer import _safe_filename
        # Same Persian morphemes, different ZWNJ placement.
        # Post-`re.sub(r"[^\w\s-]", "", text)` both collapse to identical
        # runs of Persian codepoints — but the raw inputs are distinct.
        company_a = "شرکت‌الف"    # ZWNJ between morphemes
        company_b = "شرکتالف"     # no ZWNJ

        fname_a = _safe_filename(company_a)
        fname_b = _safe_filename(company_b)
        assert fname_a != fname_b, (
            f"two Persian companies differing only in ZWNJ position "
            f"collapsed to the same filename: {fname_a!r} == {fname_b!r}. "
            f"Renderer would overwrite the first job's docs when the "
            f"second same-title job renders."
        )

    def test_zwj_position_produces_distinct_filenames(self):
        """ZWJ variant of the same collision class — the regex strip
        removes ZWJ in the middle of a name the same way it removes
        ZWNJ.
        """
        from pdf_gen.renderer import _safe_filename
        name_a = "Foo‍Corp"   # ZWJ between "Foo" and "Corp"
        name_b = "FooCorp"    # no ZWJ

        fname_a = _safe_filename(name_a)
        fname_b = _safe_filename(name_b)
        assert fname_a != fname_b, (
            f"ZWJ-differing inputs collapsed to the same filename: "
            f"{fname_a!r} == {fname_b!r}."
        )

    def test_safe_filename_is_deterministic(self):
        """Same input → same output. The hash suffix must be a pure
        function of the input, not stateful (e.g. no PID salt).
        Otherwise digest.py's attachment list and the renderer output
        path disagree.
        """
        from pdf_gen.renderer import _safe_filename
        for text in ("Acme Corp", "شرکت‌الف", "Unknown", ""):
            assert _safe_filename(text) == _safe_filename(text), (
                f"_safe_filename is non-deterministic on input {text!r}"
            )

    def test_safe_filename_never_returns_empty(self):
        """Empty input (or Cf-only input the regex strips to nothing)
        must still return a non-empty, filesystem-safe string. Pre-fix,
        `_safe_filename("​")` (ZWSP) returned `""` — the empty base
        cascaded into a filename like `_ProductManager_Resume.docx` and
        two ZWSP-companies collided.
        """
        from pdf_gen.renderer import _safe_filename
        for text in ("", "​", "‌", "‍", "​​​"):
            result = _safe_filename(text)
            assert result, (
                f"_safe_filename({text!r}) returned empty string — "
                f"downstream filename builds `_{{title}}_Resume.docx` "
                f"which silently collides across Cf-only cards."
            )
            assert result.startswith("unnamed_"), (
                f"empty-sanitized input must fall back to the 'unnamed' "
                f"placeholder before the hash tail; got {result!r}"
            )

    def test_safe_filename_shape_is_prefix_plus_hash(self):
        """Shape contract: `{sanitized}_{hex4}`. Downstream consumers
        (digest attachment names, renderer output paths) rely on the
        sanitized prefix being human-readable — the hash is a tail
        discriminator, not a wholesale hash-only filename.
        """
        from pdf_gen.renderer import _safe_filename
        result = _safe_filename("Acme Corp")
        # "Acme_Corp" + "_" + 4 hex chars = 14 total.
        assert result.startswith("Acme_Corp_"), (
            f"human-readable prefix must survive the hash suffix; "
            f"got {result!r}"
        )
        tail = result.rsplit("_", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{4}", tail), (
            f"hash tail must be exactly 4 lowercase hex chars; "
            f"got {tail!r} in {result!r}"
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
