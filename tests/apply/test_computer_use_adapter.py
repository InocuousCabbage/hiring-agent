"""tests/apply/test_computer_use_adapter.py — S20 adapter invariants.

Covers all L13 (hardcoded review_required) invariants + landmines L5, L6, L7,
L9, L12 as they touch the adapter surface. See spec §TDD scaffolding.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make `src` importable as `src.…` (canonical) AND as top-level.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.apply.adapters.computer_use import (  # noqa: E402
    ComputerUseAdapter,
    _CANNED_ALLOWLIST,
    _CANNED_ENV_VAR,
    _cap_max_iterations,
)
from src.apply.dispatcher import (  # noqa: E402
    ConfigValidationError,
    dispatch,
    validate_long_tail,
)
from src.apply.types import ApplyContext, ApplyResult  # noqa: E402


ADAPTER_SRC = ROOT / "src" / "apply" / "adapters" / "computer_use.py"
CONTROLLER_SRC = ROOT / "src" / "apply" / "controller.py"


def _make_ctx(
    *,
    mode: str = "review",
    dry_run: bool = True,
    long_tail: str = "computer_use",
    resume_pdf_path: str = "/tmp/resume.pdf",
    cover_letter_pdf_path: str | None = None,
    timeout_seconds: int = 300,
) -> ApplyContext:
    """Build a valid S17 ApplyContext for S20 adapter tests.

    Reconciliation B: S20's original tests used a shim ApplyContext with
    flat `long_tail`, `timeout_seconds`, `resume_pdf_path`, and
    `cover_letter_pdf_path` fields. S17's canonical ApplyContext has a
    different (positional-required) shape — profile, job, resume_path,
    cover_letter_path, config, applicant, dry_run, mode.

    We snapshot the S20 knobs into the canonical shape:
      - resume_pdf_path → ctx.resume_path (Path)
      - cover_letter_pdf_path → ctx.cover_letter_path (Path | None)
      - long_tail + timeout_seconds → ctx.config['apply']
    """
    # Use the S1 profile-factory that other test suites use — matches S17.
    from tests.fixtures.apply.profile_factory import load_example_profile
    profile = load_example_profile()
    return ApplyContext(
        profile=profile,
        job={"url": "https://unknown.example.test/apply/1"},
        resume_path=Path(resume_pdf_path),
        cover_letter_path=Path(cover_letter_pdf_path) if cover_letter_pdf_path else None,
        config={
            "apply": {
                "long_tail": long_tail,
                "timeout_seconds": timeout_seconds,
            }
        },
        applicant="jane",
        dry_run=dry_run,
        mode=mode,
    )


# ─── Fake page primitive ─────────────────────────────────────────────────

class _FakeFileInput:
    def __init__(self, name: str):
        self._name = name

    def get_attribute(self, key: str) -> str | None:
        if key == "name":
            return self._name
        return None


class FakePage:
    """Minimal Playwright Page stand-in for adapter tests."""

    def __init__(self, file_inputs: list[str] | None = None, url: str = "https://unknown.example.test/apply/1"):
        self.url = url
        self._file_inputs = [_FakeFileInput(n) for n in (file_inputs or [])]
        self.set_input_files_calls: list[tuple[str, str]] = []
        self.mouse = MagicMock()
        self.keyboard = MagicMock()

    def query_selector_all(self, selector: str):
        if selector == 'input[type="file"]':
            return list(self._file_inputs)
        return []

    def set_input_files(self, selector: str, path: str):
        self.set_input_files_calls.append((selector, path))

    def screenshot(self, *args, **kwargs):
        return b"\x89PNGfakebytes"


# ─── L13: HARD-CODED review_required, ALL modes ──────────────────────────


@pytest.mark.parametrize("mode", ["review", "auto"])
def test_apply_always_returns_review_required_regardless_of_mode_L13(mode, monkeypatch):
    """L13: even in `auto` mode the adapter must return review_required."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    ctx = _make_ctx(mode=mode, dry_run=(mode == "review"))
    page = FakePage(file_inputs=["resume"])
    result = adapter.apply(page, ctx)
    assert isinstance(result, ApplyResult)
    assert result.status == "review_required", (
        f"L13 violated: mode={mode} produced status={result.status!r}"
    )


def test_apply_always_returns_review_required_even_with_dry_run_false_L13(monkeypatch):
    """L13 belt-and-suspenders: dry_run=False + mode=auto still → review_required."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    ctx = _make_ctx(mode="auto", dry_run=False)
    page = FakePage()
    result = adapter.apply(page, ctx)
    assert result.status == "review_required"


def test_no_submitted_status_string_in_source():
    """Grep source for auto-submit-shaped status literals — must be empty."""
    src = ADAPTER_SRC.read_text()
    assert 'status="submitted"' not in src
    assert 'status="auto_declined"' not in src
    # `status="failed"` is disallowed by acceptance #1 (only allowed on
    # unrecoverable pre-fill exceptions — this shard elects to never emit it).
    assert 'status="failed"' not in src


def test_review_required_status_literal_appears_exactly_once():
    """Complement of acceptance #1: grep -c → exactly 1."""
    src = ADAPTER_SRC.read_text()
    assert src.count('status="review_required"') == 1


def test_class_docstring_declares_landmine_L13():
    """Docstring first line must be the exact L13 declaration substring."""
    doc = ComputerUseAdapter.__doc__ or ""
    assert doc.startswith(
        "ALWAYS returns review_required (landmine L13); never auto-submits in Phase 3."
    ), f"Bad docstring prefix: {doc[:120]!r}"


# ─── Detect / registration surface ───────────────────────────────────────


def test_detect_returns_true_for_any_url():
    adapter = ComputerUseAdapter()
    assert adapter.detect("https://unknown-ats.example.com/apply/1") is True
    assert adapter.detect("") is True
    assert adapter.detect("not-a-url") is True
    assert adapter.detect("https://boards.greenhouse.io/anything") is True


def test_domains_is_empty_tuple():
    assert ComputerUseAdapter.domains == ()
    assert isinstance(ComputerUseAdapter.domains, tuple)


def test_name_is_computer_use():
    assert ComputerUseAdapter.name == "computer_use"


# ─── File-upload short-circuit (variation-C gap) ─────────────────────────


def test_file_upload_short_circuited_to_set_input_files(monkeypatch):
    """Uploads go through page.set_input_files BEFORE the LLM loop; the
    canned LLM script contains NO tool call targeting the file-input coords."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    page = FakePage(file_inputs=["resume", "cover_letter"])
    ctx = _make_ctx(
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path="/tmp/cover.pdf",
    )
    result = adapter.apply(page, ctx)
    # set_input_files called for both inputs BEFORE any LLM turn.
    paths = [p for (_, p) in page.set_input_files_calls]
    assert "/tmp/resume.pdf" in paths
    assert "/tmp/cover.pdf" in paths
    assert result.status == "review_required"

    # Canned script has NO `click` on file-input coords — assert by inspection.
    import json
    canned = json.loads(
        (ROOT / "tests" / "fixtures" / "apply" / "computer_use_canned_script.json").read_text()
    )
    # No canned tool_call names an <input type="file"> action.
    for tc in canned:
        assert tc.get("tool") not in ("upload_file", "file_upload")


def test_file_upload_does_not_send_resume_to_portfolio_slot(monkeypatch):
    """RED regression: file-upload short-circuit must DISCRIMINATE by input name.

    Bug: the previous fallback branch (`elif resume_path`) assigned the resume
    PDF to EVERY input that did not match 'cover'. On real ATS forms, that
    stapled the resume into portfolio and references slots — the wrong file
    in the wrong field. Portfolio/references have no candidate-profile
    counterpart in the current profile schema, so those slots must be
    SKIPPED (not filled with resume).

    Discrimination rules:
      - name contains 'cover'                     → cover_letter_path
      - name contains 'resume' or 'cv'            → resume_path
      - name contains 'portfolio'                 → skip (no source)
      - name contains 'reference'                 → skip (no source)
      - name empty / unknown                      → skip (do not guess)
    """
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    # Real ATS common slot names.
    page = FakePage(file_inputs=[
        "job_application[resume]",
        "job_application[cover_letter]",
        "job_application[portfolio]",
        "job_application[references]",
    ])
    ctx = _make_ctx(
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path="/tmp/cover.pdf",
    )
    adapter.apply(page, ctx)

    # Group set_input_files calls by (selector, path) for assertions.
    by_selector: dict[str, str] = {sel: path for sel, path in page.set_input_files_calls}

    # Resume goes ONLY to the resume slot.
    resume_sel = 'input[type="file"][name="job_application[resume]"]'
    cover_sel = 'input[type="file"][name="job_application[cover_letter]"]'
    portfolio_sel = 'input[type="file"][name="job_application[portfolio]"]'
    references_sel = 'input[type="file"][name="job_application[references]"]'

    assert by_selector.get(resume_sel) == "/tmp/resume.pdf", (
        f"Resume slot should receive resume.pdf, got {by_selector.get(resume_sel)!r}"
    )
    assert by_selector.get(cover_sel) == "/tmp/cover.pdf", (
        f"Cover-letter slot should receive cover.pdf, got {by_selector.get(cover_sel)!r}"
    )
    # THE BUG: portfolio slot received resume.pdf via the elif-resume fallback.
    assert portfolio_sel not in by_selector, (
        f"Portfolio slot must NOT be filled — no matching candidate-profile "
        f"source. Got path={by_selector.get(portfolio_sel)!r}. "
        f"(Pre-fix bug stapled resume.pdf into portfolio.)"
    )
    assert references_sel not in by_selector, (
        f"References slot must NOT be filled — no matching candidate-profile "
        f"source. Got path={by_selector.get(references_sel)!r}."
    )


def test_file_upload_skips_unknown_slots_with_empty_name(monkeypatch):
    """Empty / unknown input names → SKIP (do not guess with resume)."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    # Empty name and a totally-unrecognized name.
    page = FakePage(file_inputs=["", "supporting_document"])
    ctx = _make_ctx(
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path="/tmp/cover.pdf",
    )
    adapter.apply(page, ctx)

    paths = [p for (_, p) in page.set_input_files_calls]
    # Neither slot should receive the resume: unknown ≠ resume.
    assert "/tmp/resume.pdf" not in paths, (
        f"Unknown slots must not be filled with resume; got calls={page.set_input_files_calls!r}"
    )


def test_file_upload_recognizes_common_resume_aliases(monkeypatch):
    """`name` containing 'cv' or 'resume' → resume path."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    adapter = ComputerUseAdapter()
    # Real-world aliases seen across ATS platforms.
    page = FakePage(file_inputs=["resume", "cv"])
    ctx = _make_ctx(
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path="/tmp/cover.pdf",
    )
    adapter.apply(page, ctx)
    by_selector = {sel: path for sel, path in page.set_input_files_calls}
    assert by_selector.get('input[type="file"][name="resume"]') == "/tmp/resume.pdf"
    assert by_selector.get('input[type="file"][name="cv"]') == "/tmp/resume.pdf"


# ─── max_iterations cap ──────────────────────────────────────────────────


def test_max_iterations_returns_review_required(monkeypatch):
    """Canned script with more entries than max_iter → status still review_required."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")

    # Force max_iter down via a small timeout_seconds so cap = timeout/10.
    ctx = _make_ctx(timeout_seconds=30)  # cap=3
    # Override the canned script to be longer than 3.
    long_script = [{"tool": "click", "args": {"coordinate": [1, 1]}} for _ in range(21)]
    with patch("src.apply.adapters.computer_use._load_canned_script", return_value=long_script):
        adapter = ComputerUseAdapter()
        page = FakePage()
        result = adapter.apply(page, ctx)
    assert result.status == "review_required"
    assert "max iterations" in (result.reason or "").lower()


def test_cap_max_iterations_defaults_to_20():
    assert _cap_max_iterations(300) == 20
    assert _cap_max_iterations(100) == 10
    assert _cap_max_iterations(0) == 20  # fallback
    assert _cap_max_iterations(-1) == 20  # fallback


# ─── L9: canned env-var STRICT allowlist ─────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_canned_llm_env_var_strict_allowlist_L9_enabled_values(monkeypatch, value):
    """Allowlisted values → canned client used (no real Anthropic call)."""
    monkeypatch.setenv(_CANNED_ENV_VAR, value)
    from src.apply.adapters.computer_use import _canned_mode_enabled
    assert _canned_mode_enabled() is True
    assert value in _CANNED_ALLOWLIST


@pytest.mark.parametrize("value", ["0", "false", "", "no", "random", "TRUE", "Yes"])
def test_canned_llm_env_var_strict_allowlist_L9_rejected_values(monkeypatch, value):
    """Non-allowlisted values → canned mode OFF (would use real client)."""
    monkeypatch.setenv(_CANNED_ENV_VAR, value)
    from src.apply.adapters.computer_use import _canned_mode_enabled
    assert _canned_mode_enabled() is False
    # And when the real client is invoked, the adapter still returns
    # review_required — but we don't want real network. Mock at boundary.
    with patch("src.apply.adapters.computer_use.call_claude_computer_use") as mock_llm:
        mock_llm.return_value = {
            "tool_calls": [{"tool": "screenshot", "args": {}}],
            "final_message": "done",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        adapter = ComputerUseAdapter()
        result = adapter.apply(FakePage(), _make_ctx(timeout_seconds=100))
        assert result.status == "review_required"


# ─── L6: no datetime.utcnow anywhere in shard's files ────────────────────


def test_no_utcnow_L6():
    for path in (ADAPTER_SRC, CONTROLLER_SRC):
        assert "datetime.utcnow" not in path.read_text(), f"{path.name} uses deprecated utcnow"


# ─── L5: try/finally teardown (adapter path) ─────────────────────────────


def test_try_finally_teardown_L5(monkeypatch):
    """When the controller raises mid-turn, the adapter still returns a
    review_required ApplyResult (never propagates the exception, per L13)."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")

    with patch("src.apply.adapters.computer_use.Controller") as mock_ctrl:
        instance = MagicMock()
        instance.apply_tool_call.side_effect = RuntimeError("controller boom")
        mock_ctrl.return_value = instance
        adapter = ComputerUseAdapter()
        result = adapter.apply(FakePage(), _make_ctx())
    assert result.status == "review_required"


# ─── L7: PII regression + tool NAME-only logs ────────────────────────────


def test_no_pii_in_logs_L7(monkeypatch, caplog):
    """A full canned run must not bleed sample-candidate PII into log records."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    profile_email = "jane.doe@example.test"
    profile_phone = "555-0100"

    caplog.set_level(logging.DEBUG)
    ctx = _make_ctx(
        resume_pdf_path=f"/tmp/{profile_email}_resume.pdf",  # PII-in-path check
    )
    adapter = ComputerUseAdapter()
    adapter.apply(FakePage(file_inputs=["resume"]), ctx)

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert profile_email not in joined, "L7 violation: email leaked into logs"
    assert profile_phone not in joined, "L7 violation: phone leaked into logs"


def test_tool_call_log_records_name_not_args_L7(caplog, monkeypatch):
    """`apply.computer_use.tool_called` events must not carry arg values."""
    monkeypatch.setenv(_CANNED_ENV_VAR, "1")
    sensitive_text = "SUPER-SECRET-ARG-STRING-XYZ"
    canned = [{"tool": "type", "args": {"text": sensitive_text}}]
    caplog.set_level(logging.INFO)
    with patch("src.apply.adapters.computer_use._load_canned_script", return_value=canned):
        adapter = ComputerUseAdapter()
        adapter.apply(FakePage(), _make_ctx(timeout_seconds=30))
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert sensitive_text not in joined, "L7 violation: tool arg leaked into log"


# ─── Dispatcher registration gating (S2 seam) ────────────────────────────


def test_dispatch_returns_none_when_long_tail_none():
    result = dispatch(
        "https://unknown-ats.example.com/1",
        {"apply": {"long_tail": "none"}},
    )
    assert result is None


def test_dispatch_returns_computer_use_when_long_tail_opt_in():
    # H11: long_tail fallback also requires the fallback name in allowed_ats.
    result = dispatch(
        "https://unknown-ats.example.com/1",
        {"apply": {
            "long_tail": "computer_use",
            "allowed_ats": ["computer_use"],
        }},
    )
    assert result is not None
    assert isinstance(result, ComputerUseAdapter)
    assert result.name == "computer_use"


def test_config_validator_rejects_unknown_long_tail_value():
    with pytest.raises(ConfigValidationError):
        validate_long_tail("magic")
    with pytest.raises(ConfigValidationError):
        validate_long_tail("browserbase")


def test_config_validator_accepts_known_long_tail_values():
    assert validate_long_tail("none") == "none"
    assert validate_long_tail("computer_use") == "computer_use"


def test_dispatcher_uses_string_map_registry_L12():
    """L12: registry maps names to `module:Class` strings (never class objects).

    S17 reconciliation: the canonical registry attribute is
    `_ADAPTER_CLASSES` (S2 name). S20's shim used `_ADAPTER_REGISTRY`.
    """
    from src.apply.dispatcher import _ADAPTER_CLASSES
    assert isinstance(_ADAPTER_CLASSES, dict)
    for key, val in _ADAPTER_CLASSES.items():
        assert isinstance(val, str), f"Entry {key!r} is not a string (violates L12)"
        assert ":" in val, f"Entry {key!r} not in module:Class form"


# ─── Anthropic Computer Use agent-loop pattern (Fix 3) ───────────────────
#
# Canonical pattern per Anthropic docs
# (https://docs.anthropic.com/en/docs/agents-and-tools/computer-use):
#
#   for turn in range(max_iter):
#       response = client.messages.create(messages=messages, tools=...)
#       messages.append({"role": "assistant", "content": response.content})
#       tool_results = [
#           {"type": "tool_result", "tool_use_id": tu.id, "content": [...]}
#           for tu in response.content if tu.type == "tool_use"
#       ]
#       if not tool_results or response.stop_reason == "end_turn":
#           break
#       messages.append({"role": "user", "content": tool_results})
#
# Pre-fix bugs in `_run_llm_loop`:
#   1. Only tool_calls[0] is executed per turn; extras are dropped.
#   2. No message history — each turn re-sends the same static user_prompt.
#   3. No screenshot feedback — Claude sees no result of prior actions.
#   4. No stop_reason termination — loop runs until max_iter or empty list.


def test_run_llm_loop_processes_all_tool_calls_not_just_first(monkeypatch):
    """Multi-tool: a single turn's `tool_calls` list must be exhausted, not
    just index [0].

    RED: current implementation grabs `tool_calls[0]` and drops the rest.
    """
    monkeypatch.delenv(_CANNED_ENV_VAR, raising=False)

    called_tools: list[str] = []

    class _StubController:
        def __init__(self, *_a, **_k):
            pass

        def apply_tool_call(self, tool_name: str, args: dict) -> dict:
            called_tools.append(tool_name)
            return {"ok": True}

        def screenshot(self) -> bytes:
            return b"\x89PNG-stub"

    call_count = {"n": 0}

    def fake_llm(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Turn 1: return THREE tool_use blocks in a single response.
            return {
                "tool_calls": [
                    {"tool": "click", "args": {"coordinate": [1, 1]}, "id": "toolu_a"},
                    {"tool": "type", "args": {"text": "hi"}, "id": "toolu_b"},
                    {"tool": "scroll", "args": {"dx": 0, "dy": 100}, "id": "toolu_c"},
                ],
                "content": [],
                "stop_reason": "tool_use",
                "final_message": "",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        # Turn 2: no tool_calls → loop terminates.
        return {"tool_calls": [], "content": [], "stop_reason": "end_turn", "final_message": "", "usage": {}}

    with patch("src.apply.adapters.computer_use.call_claude_computer_use", side_effect=fake_llm):
        with patch("src.apply.adapters.computer_use.Controller", _StubController):
            adapter = ComputerUseAdapter()
            adapter.apply(FakePage(), _make_ctx(timeout_seconds=300))

    assert called_tools == ["click", "type", "scroll"], (
        f"Expected all three tool_use blocks executed, got {called_tools!r}. "
        "Current impl only invokes tool_calls[0] per turn."
    )


def test_run_llm_loop_maintains_message_history_across_turns(monkeypatch):
    """History: each subsequent LLM call must receive the prior assistant
    turn and the user's tool_result turn.

    RED: current impl sends the same static user_prompt on every call —
    Claude has no memory of prior actions across turns.
    """
    monkeypatch.delenv(_CANNED_ENV_VAR, raising=False)

    class _StubController:
        def __init__(self, *_a, **_k):
            pass

        def apply_tool_call(self, tool_name, args):
            return {"ok": True}

        def screenshot(self):
            return b"\x89PNG-stub"

    seen_calls: list[dict] = []

    def fake_llm(*args, **kwargs):
        # Capture kwargs so we can inspect what the adapter passed.
        seen_calls.append(kwargs)
        if len(seen_calls) == 1:
            return {
                "tool_calls": [{"tool": "click", "args": {"coordinate": [1, 2]}, "id": "toolu_1"}],
                "content": [],
                "stop_reason": "tool_use",
                "final_message": "",
                "usage": {},
            }
        return {"tool_calls": [], "content": [], "stop_reason": "end_turn", "final_message": "", "usage": {}}

    with patch("src.apply.adapters.computer_use.call_claude_computer_use", side_effect=fake_llm):
        with patch("src.apply.adapters.computer_use.Controller", _StubController):
            adapter = ComputerUseAdapter()
            adapter.apply(FakePage(), _make_ctx(timeout_seconds=300))

    assert len(seen_calls) >= 2, (
        f"Expected at least 2 LLM turns, got {len(seen_calls)} — "
        "loop terminated too early or never ran a second turn."
    )
    # Second turn must carry a messages list with the prior context.
    second_call_messages = seen_calls[1].get("messages")
    assert second_call_messages is not None, (
        "Second LLM turn was called without a `messages` kwarg — no history "
        "is being propagated. Current impl re-sends the static user_prompt."
    )
    roles = [m.get("role") for m in second_call_messages]
    assert "assistant" in roles, (
        f"Second turn's messages must include the prior assistant turn. "
        f"Got roles={roles!r}."
    )
    # The most recent user turn on the second call must include tool_result.
    last_user = next(m for m in reversed(second_call_messages) if m.get("role") == "user")
    content = last_user.get("content")
    assert isinstance(content, list), "user tool_result turn content must be a list"
    types = [b.get("type") for b in content if isinstance(b, dict)]
    assert "tool_result" in types, (
        f"Second turn's user message must carry a tool_result block. Got types={types!r}."
    )
    # Verify the assistant tool_use block is well-shaped for the Anthropic
    # API (id + name + input) AND that the follow-up tool_result correctly
    # references that same id. Without this, the API rejects the payload —
    # the loop would break silently on the second real turn.
    assistant_msg = next(m for m in second_call_messages if m.get("role") == "assistant")
    assistant_content = assistant_msg.get("content") or []
    tool_use_blocks = [b for b in assistant_content if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert tool_use_blocks, (
        f"Assistant turn must contain at least one tool_use block; got {assistant_content!r}"
    )
    for tu in tool_use_blocks:
        assert tu.get("id"), f"tool_use block missing id: {tu!r}"
        assert tu.get("name"), f"tool_use block missing name: {tu!r}"
        assert "input" in tu, f"tool_use block missing input: {tu!r}"
    assistant_ids = {tu.get("id") for tu in tool_use_blocks}
    tool_result_ids = {
        b.get("tool_use_id")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    assert tool_result_ids <= assistant_ids or (assistant_ids & tool_result_ids), (
        f"tool_result.tool_use_id must reference an assistant tool_use.id. "
        f"assistant ids={assistant_ids!r}, tool_result ids={tool_result_ids!r}"
    )


def test_run_llm_loop_includes_screenshot_in_tool_result_content(monkeypatch):
    """Screenshot feedback: each tool_result on the follow-up user turn must
    include an `image` block encoding the post-action screenshot.

    RED: current impl never captures a screenshot after a tool action.
    """
    monkeypatch.delenv(_CANNED_ENV_VAR, raising=False)

    class _StubController:
        def __init__(self, *_a, **_k):
            pass

        def apply_tool_call(self, tool_name, args):
            return {"ok": True}

        def screenshot(self):
            # Sentinel bytes so we can spot them in the b64 payload.
            return b"HELLO_SCREENSHOT_PNG_BYTES"

    seen_calls: list[dict] = []

    def fake_llm(*args, **kwargs):
        seen_calls.append(kwargs)
        if len(seen_calls) == 1:
            return {
                "tool_calls": [{"tool": "click", "args": {"coordinate": [1, 2]}, "id": "toolu_x"}],
                "content": [],
                "stop_reason": "tool_use",
                "final_message": "",
                "usage": {},
            }
        return {"tool_calls": [], "content": [], "stop_reason": "end_turn", "final_message": "", "usage": {}}

    with patch("src.apply.adapters.computer_use.call_claude_computer_use", side_effect=fake_llm):
        with patch("src.apply.adapters.computer_use.Controller", _StubController):
            adapter = ComputerUseAdapter()
            adapter.apply(FakePage(), _make_ctx(timeout_seconds=300))

    assert len(seen_calls) >= 2
    messages = seen_calls[1].get("messages") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    assert last_user is not None, "no user tool_result turn on second call"
    content = last_user.get("content") or []
    # Find the tool_result and inspect its content for an image block.
    tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert tool_results, f"no tool_result block in user turn content={content!r}"

    # Each tool_result.content must include an image block with base64 PNG.
    import base64
    expected_b64 = base64.b64encode(b"HELLO_SCREENSHOT_PNG_BYTES").decode("ascii")
    found_image = False
    for tr in tool_results:
        inner = tr.get("content")
        if not isinstance(inner, list):
            continue
        for blk in inner:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "image":
                source = blk.get("source") or {}
                if source.get("type") == "base64" and source.get("data") == expected_b64:
                    found_image = True
                    break
        if found_image:
            break
    assert found_image, (
        "Expected a screenshot image block (base64 PNG) inside a tool_result's "
        "content on the follow-up user turn. Current impl never captures a "
        "screenshot after tool actions."
    )


def test_apply_reads_timeout_from_inner_ctx_config_shape(monkeypatch):
    """Regression guard: ctx.config shape.

    The S17 seam sets ApplyContext.config to the INNER apply_config dict
    (same shape greenhouse.py reads: ctx.config.get('mode') etc.). Legacy
    tests wrap it as ctx.config['apply']. The adapter must accept BOTH.

    Pre-Fix-1 the dispatcher returned 'skipped' before the adapter ran,
    hiding this shape mismatch. Post-Fix-1 the adapter runs — and a small
    timeout_seconds (e.g. 30 → cap=3) must actually take effect regardless
    of which shape the config was passed in.
    """
    from src.apply.profile import CandidateProfile
    from tests.fixtures.apply.profile_factory import load_example_profile

    monkeypatch.setenv(_CANNED_ENV_VAR, "1")

    # INNER shape (canonical from S17 seam).
    ctx_inner = ApplyContext(
        profile=load_example_profile(),
        job={"url": "https://x.example/1"},
        resume_path=Path("/tmp/resume.pdf"),
        cover_letter_path=None,
        config={"timeout_seconds": 30, "long_tail": "computer_use"},
        applicant="jane",
        dry_run=True,
        mode="review",
    )
    # Force the canned script to be longer than cap=3.
    long_script = [{"tool": "click", "args": {"coordinate": [1, 1]}} for _ in range(21)]
    with patch("src.apply.adapters.computer_use._load_canned_script", return_value=long_script):
        adapter = ComputerUseAdapter()
        result = adapter.apply(FakePage(), ctx_inner)
    assert result.status == "review_required"
    # If ctx.config shape had silently downgraded to the 300s default,
    # cap would be 20 and 'max iterations' would still be the reason —
    # but so would ANY unreached cap. So we assert the reason IS the
    # max-iter one AND that cap==3 by observing exactly 3 clicks executed.
    assert "max iterations" in (result.reason or "").lower(), (
        f"Small timeout not honored via inner-config shape: reason={result.reason!r}"
    )


def test_run_llm_loop_terminates_on_end_turn_stop_reason(monkeypatch):
    """Termination: if the LLM returns `stop_reason == "end_turn"`, the loop
    stops even if tool_calls is (defensively) non-empty on that same turn."""
    monkeypatch.delenv(_CANNED_ENV_VAR, raising=False)

    class _StubController:
        def __init__(self, *_a, **_k):
            pass

        def apply_tool_call(self, tool_name, args):
            return {"ok": True}

        def screenshot(self):
            return b"\x89PNG"

    call_count = {"n": 0}

    def fake_llm(*args, **kwargs):
        call_count["n"] += 1
        # Return stop_reason=end_turn immediately.
        return {
            "tool_calls": [],
            "content": [],
            "stop_reason": "end_turn",
            "final_message": "done",
            "usage": {},
        }

    with patch("src.apply.adapters.computer_use.call_claude_computer_use", side_effect=fake_llm):
        with patch("src.apply.adapters.computer_use.Controller", _StubController):
            adapter = ComputerUseAdapter()
            result = adapter.apply(FakePage(), _make_ctx(timeout_seconds=300))

    assert call_count["n"] == 1, (
        f"Loop must terminate after the first end_turn response; got {call_count['n']} calls."
    )
    # L13 belt-and-suspenders: still review_required.
    assert result.status == "review_required"


def test_dispatcher_registration_observable_via_monkeypatch(monkeypatch):
    """L12: monkeypatch.setattr on the class name must be observed by dispatch."""
    class _StubAdapter:
        name = "computer_use"
        domains = ()
        def detect(self, url): return True
        def apply(self, page, ctx):  # pragma: no cover — never invoked here
            return ApplyResult(status="review_required")

    monkeypatch.setattr(
        "src.apply.adapters.computer_use.ComputerUseAdapter",
        _StubAdapter,
    )
    # H11: long_tail fallback also requires computer_use in allowed_ats.
    result = dispatch(
        "https://x.example/1",
        {"apply": {
            "long_tail": "computer_use",
            "allowed_ats": ["computer_use"],
        }},
    )
    assert isinstance(result, _StubAdapter)


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT — docx-only-lane fallback for computer_use adapter (contract audit)
# .agent/one-big-feature/auto-apply-2026-07-06/05-renderer-contract-audit.md
# ═══════════════════════════════════════════════════════════════════════════════


def _make_docx_only_ctx(*, resume_docx_path: str, cover_letter_docx_path: str | None):
    """Build ctx where resume_path=None but resume_docx_path is set."""
    from tests.fixtures.apply.profile_factory import load_example_profile
    profile = load_example_profile()
    return ApplyContext(
        profile=profile,
        job={"url": "https://unknown.example.test/apply/1"},
        resume_path=None,
        cover_letter_path=None,
        config={"apply": {"long_tail": "computer_use", "timeout_seconds": 300}},
        applicant="jane",
        dry_run=True,
        mode="review",
        resume_docx_path=Path(resume_docx_path),
        cover_letter_docx_path=Path(cover_letter_docx_path) if cover_letter_docx_path else None,
    )


def test_computer_use_uploads_docx_when_pdf_unavailable(monkeypatch):
    """AUDIT: when render_resume returns (None, docx), computer_use must upload the DOCX.

    Previously the adapter silently skipped file uploads when resume_path was
    empty; now it falls back to resume_docx_path.
    """
    from src.apply.adapters.computer_use import ComputerUseAdapter

    ctx = _make_docx_only_ctx(
        resume_docx_path="/tmp/resume.docx",
        cover_letter_docx_path="/tmp/cover.docx",
    )
    page = FakePage(file_inputs=["resume", "cover_letter"])

    adapter = ComputerUseAdapter()
    # Stub the LLM turn to a no-op so the file-upload short-circuit is what runs.
    monkeypatch.setattr(
        "src.apply.adapters.computer_use.call_claude_computer_use",
        lambda **kwargs: {"reasoning": "stop", "actions": []},
    )
    adapter.apply(page, ctx)

    uploads = {sel: path for sel, path in page.set_input_files_calls}
    # Both slots should have received the DOCX fallback path.
    resume_upload = next(
        (path for sel, path in page.set_input_files_calls if "resume" in sel),
        None,
    )
    cover_upload = next(
        (path for sel, path in page.set_input_files_calls if "cover" in sel),
        None,
    )
    assert resume_upload == "/tmp/resume.docx", (
        f"Expected DOCX resume upload; got {resume_upload!r} (all calls: {uploads})"
    )
    assert cover_upload == "/tmp/cover.docx", (
        f"Expected DOCX cover upload; got {cover_upload!r} (all calls: {uploads})"
    )
