"""
Shared Claude LLM helper — supports two modes:

1. Claude CLI (default) — uses your Claude subscription via `claude -p --bare`
2. Anthropic API — uses ANTHROPIC_API_KEY env var with the anthropic SDK

Set ANTHROPIC_API_KEY in .env to use the API directly.
Otherwise, ensure `claude` CLI is installed and authenticated (`claude login`).
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()

# S20: canned-LLM env var — L9 strict allowlist (never truthy-check).
_APPLY_CANNED_ENV_VAR = "HIRING_AGENT_APPLY_CANNED_LLM"
_APPLY_CANNED_ALLOWLIST: tuple[str, ...] = ("1", "true", "yes")

# Map full model identifiers to CLI-friendly short names
_CLI_MODEL_MAP = {
    "claude-haiku-4-5-20251001": "haiku",
    "claude-sonnet-4-6": "sonnet",
    "claude-sonnet-4-5-20250929": "sonnet",
    "claude-3-haiku-20240307": "haiku",
    "claude-3-5-sonnet-20241022": "sonnet",
}

# Map short names to full API model identifiers
_API_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


def _use_api() -> bool:
    """Check if we should use the Anthropic API (key is set) vs CLI."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _resolve_cli_model(model: str) -> str:
    return _CLI_MODEL_MAP.get(model, model)


def _resolve_api_model(model: str) -> str:
    # If it's a short name, expand it; otherwise use as-is
    return _API_MODEL_MAP.get(model, model)


def _call_via_api(prompt: str, model: str, system: str | None, timeout: int) -> str:
    """Call Claude via the Anthropic Python SDK."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic\n"
            "Or remove ANTHROPIC_API_KEY from .env to use the Claude CLI instead."
        )

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    api_model = _resolve_api_model(model)

    kwargs = {
        "model": api_model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    log.debug("llm.call_api", model=api_model, prompt_len=len(prompt))

    response = client.messages.create(**kwargs)
    return response.content[0].text


def _call_via_cli(prompt: str, model: str, system: str | None, timeout: int) -> str:
    """Call Claude via CLI subprocess. Uses temp file for long prompts."""
    model = _resolve_cli_model(model)

    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n---\n\n{prompt}"

    log.debug("llm.call_cli", model=model, prompt_len=len(full_prompt))

    # For long prompts, write to temp file to avoid OS argument length limits
    # and CLI parsing issues. Threshold: 8000 chars (~safe ARG_MAX margin).
    try:
        if len(full_prompt) > 8000:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix="claude_prompt_"
            ) as f:
                f.write(full_prompt)
                tmp_path = f.name
            try:
                # Argv form (no shell=True): feed the prompt file directly as
                # the child's stdin. A TimeoutExpired SIGKILL then targets the
                # `claude` child itself — no orphaned grandchild via a shell
                # pipeline (M10).
                with open(tmp_path, "rb") as stdin_f:
                    result = subprocess.run(
                        ["claude", "-p"],
                        stdin=stdin_f,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
            finally:
                os.unlink(tmp_path)
        else:
            result = subprocess.run(
                ["claude", "-p", full_prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        # Redact prompt from exception — TimeoutExpired.cmd contains the full
        # argv (including the prompt when passed inline). Never let str(exc)
        # leak the prompt into logs or downstream error messages.
        log.error("llm.cli_timeout", model=model, timeout=exc.timeout, prompt_len=len(full_prompt))
        raise RuntimeError(
            f"Claude CLI timed out after {exc.timeout}s (model={model}, prompt_len={len(full_prompt)})"
        ) from None

    if result.returncode != 0:
        log.error("llm.cli_failed", returncode=result.returncode, stderr=result.stderr[:500])
        raise RuntimeError(f"Claude CLI failed (rc={result.returncode}): {result.stderr[:500]}")

    return result.stdout


def call_claude(
    prompt: str,
    model: str = "haiku",
    system: str | None = None,
    timeout: int = 300,
) -> str:
    """
    Call Claude via the best available method.

    If ANTHROPIC_API_KEY is set in the environment, uses the Anthropic SDK.
    Otherwise, uses the Claude CLI (requires `claude login` first).

    Args:
        prompt: The user prompt text.
        model: Model name (full or short like "haiku", "sonnet").
        system: Optional system prompt.
        timeout: Timeout in seconds.

    Returns:
        The model's text response.
    """
    if _use_api():
        return _call_via_api(prompt, model, system, timeout)
    return _call_via_cli(prompt, model, system, timeout)


# ──────────────────────────────────────────────────────────────────────────
# S20: Anthropic Computer Use tool loop entry point.
#
# The wider Computer Use loop (screenshot → tool_use → tool_result cycle) is
# driven by src/apply/adapters/computer_use.py + src/apply/controller.py.
# This function is the LLM-facing turn: one call in, one dict out. The
# adapter iterates and enforces the L13 review_required policy.
#
# Design:
#   - L8: JSON extracted from free-text via `json.JSONDecoder.raw_decode` on
#     the first open brace, with a balanced-brace fallback. Never a greedy
#     DOTALL brace-match regex.
#   - L9: canned mode is enabled ONLY when
#     `HIRING_AGENT_APPLY_CANNED_LLM in ("1", "true", "yes")`. Any other
#     value falls through to the real client. Callers can also pass
#     `canned_fixture=...` explicitly for unit tests.
#   - NO new package dependency. Reuses the existing `anthropic>=0.39.0`.
# ──────────────────────────────────────────────────────────────────────────

# Default canned-script fixture path (walk up from src/llm.py to repo root).
_APPLY_CANNED_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "apply" / "computer_use_canned_script.json"


def _apply_canned_mode_enabled() -> bool:
    """L9: strict allowlist — matches whitelist values exactly, nothing else."""
    return os.environ.get(_APPLY_CANNED_ENV_VAR, "") in _APPLY_CANNED_ALLOWLIST


def _load_default_canned_script() -> list[dict]:
    if _APPLY_CANNED_FIXTURE.exists():
        with _APPLY_CANNED_FIXTURE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.get("tool_calls", []))
    return []


def _extract_json_objects(text: str) -> list[dict]:
    """L8: extract every JSON object embedded in `text` via raw_decode.

    Walks each open brace and tries `JSONDecoder.raw_decode`. On success,
    records the object and continues past its end. On failure, advances one
    char. Returns [] for text with no valid JSON. NEVER uses a greedy DOTALL
    open-brace/close-brace regex — which fails on trailing prose or multiple
    objects.
    """
    out: list[dict] = []
    if not text:
        return out
    decoder = json.JSONDecoder()
    idx = text.find("{")
    n = len(text)
    while 0 <= idx < n:
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            out.append(obj)
        idx = text.find("{", max(end, idx + 1))
    return out


def _normalize_content_block(block) -> tuple[str, dict | str]:
    """Return `(kind, payload)` for a response content block.

    kind ∈ {"tool_use", "text"}; payload is a dict for tool_use or str for text.
    Handles both the Anthropic SDK object shape and canned-dict shape.
    """
    if isinstance(block, dict):
        btype = block.get("type", "")
        if btype == "tool_use":
            return "tool_use", {
                "tool": block.get("name") or block.get("tool") or "",
                "args": block.get("input") or block.get("args") or {},
                "id": block.get("id"),
            }
        if btype == "text":
            return "text", str(block.get("text", ""))
        return "", ""
    # Anthropic SDK object
    btype = getattr(block, "type", "")
    if btype == "tool_use":
        return "tool_use", {
            "tool": getattr(block, "name", "") or "",
            "args": getattr(block, "input", None) or {},
            "id": getattr(block, "id", None),
        }
    if btype == "text":
        return "text", getattr(block, "text", "") or ""
    return "", ""


def _canned_response(canned) -> dict:
    """Normalize a canned fixture into the response envelope.

    Fixtures may be:
      - a list of tool-call dicts
      - a dict with "tool_calls" / "final_message" / "usage" keys
      - a dict with "content" (Anthropic-shaped list of blocks)
    """
    if canned is None:
        canned = _load_default_canned_script()

    if isinstance(canned, list):
        return {
            "tool_calls": list(canned),
            "final_message": "canned",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    if not isinstance(canned, dict):
        return {"tool_calls": [], "final_message": "", "usage": {}}

    if "tool_calls" in canned:
        return {
            "tool_calls": list(canned.get("tool_calls") or []),
            "final_message": str(canned.get("final_message", "")),
            "usage": dict(canned.get("usage") or {}),
        }

    # Fall back to parsing an Anthropic-shaped `content` list.
    return _parse_response_dict(canned)


def _parse_response_dict(response: dict) -> dict:
    tool_calls: list[dict] = []
    text_parts: list[str] = []
    for block in response.get("content", []) or []:
        kind, payload = _normalize_content_block(block)
        if kind == "tool_use" and isinstance(payload, dict):
            tool_calls.append(payload)
        elif kind == "text" and isinstance(payload, str):
            text_parts.append(payload)
            # L8: also mine embedded JSON tool-call blobs from free-text.
            for obj in _extract_json_objects(payload):
                if _looks_like_tool_call(obj):
                    tool_calls.append(_coerce_tool_call(obj))
    usage = response.get("usage") or {}
    return {
        "tool_calls": tool_calls,
        "final_message": "\n".join(text_parts),
        "usage": {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        },
    }


def _looks_like_tool_call(obj: dict) -> bool:
    """A dict is tool-call-shaped if it has one of the identifying keys."""
    return isinstance(obj, dict) and (
        "tool" in obj or "action" in obj or "name" in obj
    )


def _coerce_tool_call(obj: dict) -> dict:
    tool = obj.get("tool") or obj.get("action") or obj.get("name") or ""
    args = obj.get("args")
    if args is None:
        args = obj.get("input")
    if args is None:
        args = {k: v for k, v in obj.items() if k not in ("tool", "action", "name")}
    return {"tool": tool, "args": args}


def _parse_sdk_response(response) -> dict:
    """Normalize an anthropic SDK response object into our dict envelope."""
    tool_calls: list[dict] = []
    text_parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        kind, payload = _normalize_content_block(block)
        if kind == "tool_use" and isinstance(payload, dict):
            tool_calls.append(payload)
        elif kind == "text" and isinstance(payload, str):
            text_parts.append(payload)
            for obj in _extract_json_objects(payload):
                if _looks_like_tool_call(obj):
                    tool_calls.append(_coerce_tool_call(obj))
    usage = getattr(response, "usage", None)
    usage_dict = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    } if usage else {"input_tokens": 0, "output_tokens": 0}
    return {
        "tool_calls": tool_calls,
        "final_message": "\n".join(text_parts),
        "usage": usage_dict,
    }


def call_claude_computer_use(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    *,
    max_iterations: int = 20,
    canned_fixture: dict | list | None = None,
    messages: list[dict] | None = None,
) -> dict:
    """One turn of an Anthropic Computer Use tool loop.

    Returns a dict of shape:
        {"tool_calls": list[dict], "final_message": str, "usage": dict,
         "stop_reason": str}

    Canned-mode selection (L9): canned client is used when
    `HIRING_AGENT_APPLY_CANNED_LLM` is in the strict allowlist
    ("1", "true", "yes"), OR when `canned_fixture` is passed explicitly.
    Any OTHER value (including "0", "false", empty string, "no", "random")
    routes to the real Anthropic client.

    max_iterations is passed through — the caller (adapter) enforces the
    per-loop cap; this function is a single turn.

    `messages` is an optional multi-turn history list. When provided, it
    replaces the single-turn `[{"role": "user", "content": user_prompt}]`
    default. The caller owns turn-by-turn assembly of assistant tool_use
    blocks and follow-up user tool_result blocks — this function is a
    pure per-turn transport that echoes back the parsed response plus
    `stop_reason` so the caller can drive the canonical agent loop.

    Uses the existing `anthropic>=0.39.0` dep — no new package added by
    this shard (S20).
    """
    if _apply_canned_mode_enabled() or canned_fixture is not None:
        return _canned_response(canned_fixture)

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover — dep is in requirements.txt
        raise RuntimeError(
            "anthropic SDK required for computer use. pip install anthropic"
        ) from exc

    client = anthropic.Anthropic()
    api_model = _resolve_api_model("sonnet")
    # Multi-turn history takes precedence over the single-turn user_prompt so
    # the caller can drive the canonical Anthropic agent loop.
    turn_messages = (
        list(messages)
        if messages is not None
        else [{"role": "user", "content": user_prompt}]
    )
    kwargs = {
        "model": api_model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": turn_messages,
    }
    if tools:
        kwargs["tools"] = tools
        # Detect a computer_use tool declaration and add its beta header.
        for t in tools:
            if isinstance(t, dict) and str(t.get("type", "")).startswith("computer_"):
                kwargs["extra_headers"] = {"anthropic-beta": "computer-use-2025-11-24"}
                break

    log.debug("llm.computer_use_call", model=api_model, tools=len(tools or []))
    response = client.messages.create(**kwargs)
    parsed = _parse_sdk_response(response)
    # Echo stop_reason so the agent loop can terminate on 'end_turn'.
    parsed["stop_reason"] = str(getattr(response, "stop_reason", "") or "")
    return parsed
