"""
tests/test_pre_auto_apply_fixes.py — Pin down behavior of the Phase 1.5
pre-auto-apply blockers so the fixes cannot silently regress.

Covers:
  Fix 2 (llm.py TimeoutExpired):  prompt content never appears in the
                                  RuntimeError raised on CLI timeout.
  Fix 5 (deploy flock):           cron entry uses flock -n on a pidfile so
                                  overlapping cron ticks skip.

Fix 3 (renderer PDF hard-fail) tests intentionally omitted: origin/main
refactored render_resume_pdf / render_cover_letter_pdf into dual-output
render_resume / render_cover_letter returning tuple[Optional[Path], Path],
where returning None,docx is the deliberate signal that PDF conversion
was unavailable — not a silent bug. Downstream _build_attachments filters
None. Re-introducing hard-fail would break the dual-output contract.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Fix 2: TimeoutExpired prompt redaction ────────────────────────────────────

class TestTimeoutExpiredScrub:
    """The prompt is passed as an argv element for short prompts, which means
    TimeoutExpired.cmd contains it. str(exc) would leak it into logs. The fix
    catches TimeoutExpired at the source and raises a redacted RuntimeError."""

    _SECRET = "SUPER_SECRET_PROMPT_MARKER"

    def _run_and_capture(self, prompt: str):
        import llm  # local import — needs sys.path patched above
        with patch("llm.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["claude", "-p", prompt],
                timeout=5,
            )
            with pytest.raises(RuntimeError) as exc_info:
                llm._call_via_cli(prompt, "haiku", None, timeout=5)
        return str(exc_info.value)

    def test_short_prompt_argv_path_redacts(self):
        # Short prompt (< 8000 chars) uses argv path. TimeoutExpired.cmd holds prompt.
        prompt = self._SECRET * 50  # ~1300 chars
        msg = self._run_and_capture(prompt)
        assert self._SECRET not in msg, f"Prompt leaked into RuntimeError: {msg[:200]}"
        assert "timed out" in msg.lower()
        # Metadata is fine — length is not the secret.
        assert "prompt_len=" in msg

    def test_long_prompt_shell_path_redacts(self):
        # Long prompt (> 8000 chars) uses tempfile path. Still shouldn't leak.
        prompt = self._SECRET * 500  # ~12500 chars
        msg = self._run_and_capture(prompt)
        assert self._SECRET not in msg, f"Prompt leaked into RuntimeError: {msg[:200]}"
        assert "timed out" in msg.lower()

    def test_exception_chain_broken(self):
        # `raise ... from None` should drop the TimeoutExpired __cause__ so
        # even downstream traceback formatters don't re-surface the argv.
        import llm
        with patch("llm.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["claude", "-p", self._SECRET * 50], timeout=5,
            )
            try:
                llm._call_via_cli(self._SECRET * 50, "haiku", None, timeout=5)
            except RuntimeError as exc:
                assert exc.__cause__ is None
                assert exc.__suppress_context__ is True


# ── Fix 5: deploy/cron_entry.sh flock guard ───────────────────────────────────

class TestCronEntryFlock:
    """Static assertions on the deploy script — we can't easily spin up a
    real cron in a unit test, but we can guarantee the produced CRON_CMD
    includes flock -n on a pidfile and the setup script fails cleanly if
    flock is unavailable."""

    CRON_SCRIPT = ROOT / "deploy" / "cron_entry.sh"

    def test_cron_cmd_uses_flock_non_blocking(self):
        content = self.CRON_SCRIPT.read_text()
        # Must call flock with -n (non-blocking) on the pidfile.
        assert "flock -n" in content, "cron entry must use flock -n"
        assert "hiring-agent.lock" in content, "cron entry must use a named pidfile"

    def test_cron_cmd_uses_exit_zero_on_lock_held(self):
        # -E 0 keeps cron quiet when the lock is held (previous run still working).
        content = self.CRON_SCRIPT.read_text()
        assert "-E 0" in content, "cron entry must map lock-held exit to 0"

    def test_script_errors_when_flock_missing(self, tmp_path):
        # Run the setup script with a PATH that has bash but not flock —
        # it should fail loudly rather than install a broken crontab entry.
        import os
        import shutil

        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        # Symlink only bash + basic tools (dirname, pwd) so the script can
        # start, but NOT flock or crontab.
        for tool in ("bash", "dirname", "pwd", "cd"):
            src = shutil.which(tool)
            if src:
                os.symlink(src, stub_dir / tool)

        result = subprocess.run(
            ["bash", str(self.CRON_SCRIPT)],
            env={"PATH": str(stub_dir), "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, f"Expected failure, got: {result.stdout}"
        assert "flock not found" in result.stderr
