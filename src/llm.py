"""
Shared Claude LLM helper — supports two modes:

1. Claude CLI (default) — uses your Claude subscription via `claude -p --bare`
2. Anthropic API — uses ANTHROPIC_API_KEY env var with the anthropic SDK

Set ANTHROPIC_API_KEY in .env to use the API directly.
Otherwise, ensure `claude` CLI is installed and authenticated (`claude login`).
"""

import os
import subprocess
import tempfile

import structlog

log = structlog.get_logger()

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
    if len(full_prompt) > 8000:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="claude_prompt_"
        ) as f:
            f.write(full_prompt)
            tmp_path = f.name
        try:
            # Read prompt from file via shell redirection
            result = subprocess.run(
                f'cat "{tmp_path}" | claude -p',
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=True,
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
