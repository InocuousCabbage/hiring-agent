"""tests/apply/test_llm_computer_use.py — S20 call_claude_computer_use()."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import llm  # noqa: E402
from llm import (  # noqa: E402
    _APPLY_CANNED_ALLOWLIST,
    _APPLY_CANNED_ENV_VAR,
    _extract_json_objects,
    call_claude_computer_use,
)


# ─── L9: canned env var strict allowlist at the llm.py boundary ──────────


@pytest.mark.parametrize("val", ["1", "true", "yes"])
def test_call_claude_computer_use_uses_canned_when_env_var_allowlist(monkeypatch, val):
    """Allowlisted env values → canned path; real client never constructed."""
    monkeypatch.setenv(_APPLY_CANNED_ENV_VAR, val)
    # Sentinel: patch anthropic.Anthropic to raise. If canned path is taken
    # (correct behavior) the sentinel is never invoked. If the real path is
    # taken by mistake, the test fails with the sentinel exception.
    with patch("anthropic.Anthropic", side_effect=AssertionError("real client called!")):
        out = call_claude_computer_use(
            system_prompt="s", user_prompt="u", tools=[]
        )
    assert isinstance(out, dict)
    assert "tool_calls" in out
    assert "final_message" in out
    assert "usage" in out


@pytest.mark.parametrize("val", ["0", "false", "", "no", "random", "TRUE"])
def test_call_claude_computer_use_rejects_non_allowlist_env(monkeypatch, val):
    """Non-allowlisted values → real client path taken (we mock it)."""
    monkeypatch.setenv(_APPLY_CANNED_ENV_VAR, val)

    class _StubUsage:
        input_tokens = 5
        output_tokens = 7

    class _StubTextBlock:
        type = "text"
        text = "hi"

    class _StubResponse:
        content = [_StubTextBlock()]
        usage = _StubUsage()
        stop_reason = "end_turn"

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _StubResponse()

    with patch("anthropic.Anthropic", return_value=fake_client):
        out = call_claude_computer_use(
            system_prompt="s", user_prompt="u", tools=[]
        )
    fake_client.messages.create.assert_called_once()
    assert out["final_message"] == "hi"
    assert out["usage"]["input_tokens"] == 5


def test_call_claude_computer_use_explicit_fixture_wins_over_env(monkeypatch):
    """Explicit canned_fixture param routes to canned path regardless of env."""
    monkeypatch.setenv(_APPLY_CANNED_ENV_VAR, "0")
    fixture = [{"tool": "screenshot", "args": {}}]
    out = call_claude_computer_use(
        system_prompt="s", user_prompt="u", tools=[], canned_fixture=fixture,
    )
    assert out["tool_calls"] == fixture


def test_allowlist_matches_shared_constant():
    assert _APPLY_CANNED_ALLOWLIST == ("1", "true", "yes")


# ─── L8: JSON extraction survives trailing prose ─────────────────────────


def test_llm_json_extraction_survives_trailing_prose_L8():
    text = '{"tool":"click","args":{"coordinate":[10,20]}} thanks!'
    out = _extract_json_objects(text)
    assert len(out) == 1
    assert out[0]["tool"] == "click"


def test_llm_json_extraction_multiple_objects_L8():
    text = 'first {"a":1} middle {"b":2} tail'
    out = _extract_json_objects(text)
    assert {"a": 1} in out
    assert {"b": 2} in out


def test_llm_json_extraction_no_json_returns_empty_L8():
    assert _extract_json_objects("no braces here") == []
    assert _extract_json_objects("") == []


def test_llm_json_extraction_survives_malformed_prefix_L8():
    """{"almost}"" is malformed; parser advances to the next valid `{`."""
    text = '{"almost}" {"real":true}'
    out = _extract_json_objects(text)
    assert {"real": True} in out


def test_llm_json_extraction_uses_raw_decode_not_greedy_regex():
    """Grep source: no greedy `\\{.*\\}` DOTALL pattern in llm.py."""
    src = (ROOT / "src" / "llm.py").read_text()
    assert "\\{.*\\}" not in src
    assert "json.JSONDecoder" in src


# ─── No new dependency added ─────────────────────────────────────────────


def test_no_new_dependency_added():
    """anthropic count unchanged; no new package added under this shard."""
    req = (ROOT / "requirements.txt").read_text().splitlines()
    anthropic_lines = [ln for ln in req if ln.strip().startswith("anthropic")]
    assert len(anthropic_lines) == 1, f"Expected 1 anthropic line, got {anthropic_lines}"


def test_no_new_dependency_via_git_diff():
    """Diff requirements.txt against the S17 tip → S20 adds no production deps.

    Reconciliation B: the original write-time target was `feat/auto-apply-mvp`
    (pre-shard base), but the merged tree now includes S1-S18 additions (browserbase,
    keyring, cryptography, pytest-playwright). The invariant we want to
    guard is S20-specific: from the S17 union-merge tip forward, S20 must
    add no new package. Reconciliation A's `pytest-playwright>=0.4` (an S18
    test-infra dep landed at Reconciliation A) is allowlisted for the same
    reason.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "mvp-s17-seam-wiring", "--", "requirements.txt"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        pytest.skip("git not available on this runner")
    if result.returncode != 0:
        pytest.skip(f"git diff failed: {result.stderr[:120]}")
    diff = result.stdout
    # Non-S20 additions allowlisted at Reconciliation A/B merge (S18 test infra).
    _NON_S20_ADDITIONS = {"+pytest-playwright>=0.4"}
    # No `+<pkg>>=<ver>` lines that add a new package.
    added = [ln for ln in diff.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    for ln in added:
        if ln in _NON_S20_ADDITIONS:
            continue
        assert not ln.startswith("+") or ln.startswith("+#") or ln.strip() in ("+",), (
            f"S20 must not add a dependency; got: {ln!r}"
        )


# ─── L7: PII regression at the llm.py boundary ───────────────────────────


def test_call_claude_computer_use_pii_regression_L7(caplog, monkeypatch):
    """Canned path with PII-shaped fixture must not bleed PII into logs."""
    monkeypatch.setenv(_APPLY_CANNED_ENV_VAR, "1")
    caplog.set_level(logging.DEBUG)
    fixture = [{"tool": "type", "args": {"text": "jane@example.com and 555-0100"}}]
    call_claude_computer_use(
        system_prompt="s",
        user_prompt="u",
        tools=[],
        canned_fixture=fixture,
    )
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "jane@example.com" not in joined
    assert "555-0100" not in joined


# ─── Response shape ──────────────────────────────────────────────────────


def test_call_claude_computer_use_returns_expected_shape():
    out = call_claude_computer_use(
        system_prompt="s", user_prompt="u", tools=[],
        canned_fixture=[{"tool": "screenshot", "args": {}}],
    )
    assert set(out.keys()) >= {"tool_calls", "final_message", "usage"}
    assert isinstance(out["tool_calls"], list)
