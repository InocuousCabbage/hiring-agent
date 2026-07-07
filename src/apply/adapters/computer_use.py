"""apply.adapters.computer_use — S20 Anthropic Computer Use long-tail fallback.

Grafted from variation-C as an OPT-IN fallback for unmatched ATS domains.
Gated on `apply.long_tail == "computer_use"` (default: "none"). Ignored by
the dispatcher when `long_tail == "none"`.

H10 exemption note: computer_use has no navigation-shaped page.goto call
worth wrapping with @navigation_retry. All navigation for the computer_use
adapter is driven by the LLM through Controller (see controller.py), which
owns its own timeout/retry semantics (ControllerTimeoutError). Applying
@navigation_retry at this layer would double-retry the LLM step. The submit
invariant is enforced by the L13 hard-coded review-required return — the
adapter never emits the submitted status literal so there is no submit
call site to mark with @submit_no_retry.

LANDMINE L13 (HARD-CODED, NON-CONFIGURABLE):
  This adapter's `apply()` method ALWAYS returns an ApplyResult whose status
  is the review-required literal. There is no branch, no config flag, and no
  combination of `apply.mode` / `apply.dry_run` that yields any other status
  literal. The review-required status kwarg-literal appears exactly ONCE in
  this file (the single `return` inside `apply()`). The auto-submit /
  auto-decline / failed status kwarg-literals never appear at all.

  See tests/apply/test_computer_use_adapter.py for the grep-based invariants
  (test_no_submitted_status_string_in_source and its neighbors).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

# H8: word-boundary regexes for slot-matching. Substring match ('cover' in
# name_attr) hit false positives like `coverage_letter` and
# `portfolio_cv_samples`. Match only discrete tokens.
_COVER_TOKEN_RE = re.compile(r"\bcover[_-]?letter\b|\bcoverletter\b", re.IGNORECASE)
_RESUME_TOKEN_RE = re.compile(
    r"\b(?:resume|cv|curriculum[_-]?vitae)\b", re.IGNORECASE
)

from src.apply.controller import Controller, ControllerTimeoutError
from src.apply.logging import install_scrubber
from src.apply.types import ApplyContext, ApplyResult
from src.llm import call_claude_computer_use

# S17 install_scrubber returns None (idempotent global installer).
# Install once at import time, then bind the logger separately.
install_scrubber()
log = structlog.get_logger(__name__)


# L9: canned-LLM env var — strict allowlist (never truthy-check).
_CANNED_ENV_VAR = "HIRING_AGENT_APPLY_CANNED_LLM"
_CANNED_ALLOWLIST: tuple[str, ...] = ("1", "true", "yes")

# max_iterations default (per acceptance #10). Capped at timeout_seconds / 10.
_DEFAULT_MAX_ITERATIONS = 20

# Fixture path (canned LLM tool-call script). Discovery is by walk-up from
# this file's directory to the repo root.
_FIXTURE_CANNED_SCRIPT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "apply" / "computer_use_canned_script.json"


def _canned_mode_enabled() -> bool:
    """L9: strict allowlist — never `bool(os.environ.get(...))` truthy-check."""
    return os.environ.get(_CANNED_ENV_VAR, "") in _CANNED_ALLOWLIST


def _load_canned_script() -> list[dict]:
    if _FIXTURE_CANNED_SCRIPT.exists():
        with _FIXTURE_CANNED_SCRIPT.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.get("tool_calls", []))
    return []


def _cap_max_iterations(timeout_seconds: int, default: int = _DEFAULT_MAX_ITERATIONS) -> int:
    """Cap max_iterations at `timeout_seconds / 10` (rough token/sec budget)."""
    if not timeout_seconds or timeout_seconds <= 0:
        return default
    cap = max(1, int(timeout_seconds) // 10)
    return min(default, cap)


class ComputerUseAdapter:
    """ALWAYS returns review_required (landmine L13); never auto-submits in Phase 3.

    This docstring's first line is asserted verbatim by
    `test_class_docstring_declares_landmine_L13`. Do not edit the wording
    without updating the test.

    Registered under name `"computer_use"` with empty `domains` (catch-all).
    Dispatcher gates registration on `apply.long_tail == "computer_use"`.
    """

    name: str = "computer_use"
    domains: tuple[str, ...] = ()  # catch-all; dispatcher gates on apply.long_tail

    def detect(self, url: str) -> bool:  # noqa: ARG002 — catch-all: url ignored
        """Catch-all: returns True for any URL (dispatcher enforces long_tail gate)."""
        return True

    def apply(self, page, ctx: ApplyContext) -> ApplyResult:
        """L13: HARDCODED review_required. No code path yields any other status.

        Pipeline:
          1. File-upload short-circuit: scan `<input type="file">` and hand
             each one back to Playwright's `set_input_files` directly. The
             LLM is NEVER asked to click a file input.
          2. LLM loop via Controller, capped at min(20, timeout_seconds/10).
          3. Return an ApplyResult whose status is the review-required literal.
        """
        started = datetime.now(timezone.utc)
        # Timeout comes from ctx.config. The S17 seam sets ctx.config to the
        # INNER apply_config dict (same shape greenhouse.py reads —
        # ctx.config.get("mode") etc.). Legacy tests wrap it as
        # ctx.config['apply']. Accept BOTH shapes so a Fix-1 shape-adjacent
        # bug can't silently downgrade timeout_seconds to the 300 default.
        timeout_seconds = getattr(ctx, "timeout_seconds", None)
        if timeout_seconds is None:
            cfg = getattr(ctx, "config", None) or {}
            if isinstance(cfg, dict):
                wrapped = cfg.get("apply")
                apply_cfg = wrapped if isinstance(wrapped, dict) else cfg
                timeout_seconds = apply_cfg.get("timeout_seconds", 300)
            else:
                timeout_seconds = 300
        max_iter = _cap_max_iterations(timeout_seconds)
        reason = "review_required by policy (S20 opt-in fallback)"

        try:
            self._file_upload_short_circuit(page, ctx)
            controller = Controller(page, timeout_s=30.0)
            iterations_used = self._run_llm_loop(controller, ctx, max_iter)
            if iterations_used >= max_iter:
                reason = "max iterations reached"
        except ControllerTimeoutError as exc:
            reason = f"controller timeout on tool {exc.tool_name}"
        except Exception as exc:  # noqa: BLE001 — L13: never propagate; still return review_required
            log.warning(
                "apply.computer_use.pre_fill_error",
                err_type=type(exc).__name__,
            )
            reason = f"pre_fill_error: {type(exc).__name__}"

        duration_s = (datetime.now(timezone.utc) - started).total_seconds()
        human_review_url = self._safe_page_url(page)
        log.info(
            "apply.computer_use.review_required_returned",
            reason=reason,
            duration_s=duration_s,
        )

        # ── L13: THE SINGLE status LITERAL IN THIS FILE ──────────────────
        # DO NOT add any other `status=` return in this method or the file.
        # DO NOT wrap in a branch on `ctx.mode` or `ctx.dry_run`.
        # Field-name reconciliation: S17 canonical ApplyResult uses `ats`
        # (not S20-shim `adapter_name`). See Reconciliation B in the
        # review-loop synthesis.
        return ApplyResult(
            status="review_required",
            ats=self.name,
            reason=reason,
            human_review_url=human_review_url,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _file_upload_short_circuit(self, page, ctx: ApplyContext) -> None:
        """Scan for `<input type="file">` and drive uploads via Playwright directly.

        The LLM is NEVER instructed to click a file input; this method runs
        BEFORE the LLM loop starts. This closes variation-C's file-upload gap.
        """
        try:
            inputs = page.query_selector_all('input[type="file"]')
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "apply.computer_use.file_short_circuit_scan_failed",
                err_type=type(exc).__name__,
            )
            return

        # AUDIT: dual-output renderer contract widened resume_path/cover_letter_path
        # to Path | None. When PDF is unavailable, fall back to DOCX
        # (resume_docx_path / cover_letter_docx_path per 05-renderer-contract-audit.md).
        # Tolerate the S20 shim's str-typed resume_pdf_path/cover_letter_pdf_path
        # for legacy tests.
        resume_path = getattr(ctx, "resume_pdf_path", None)
        if resume_path is None:
            resume_path = getattr(ctx, "resume_path", None)
        if not resume_path:
            # PDF unavailable — fall back to DOCX.
            resume_path = getattr(ctx, "resume_docx_path", None)
        resume_path = str(resume_path) if resume_path else ""

        cover_path = getattr(ctx, "cover_letter_pdf_path", None)
        if cover_path is None:
            cover_path = getattr(ctx, "cover_letter_path", None)
        if not cover_path:
            # PDF unavailable — fall back to DOCX.
            cover_path = getattr(ctx, "cover_letter_docx_path", None)
        cover_path = str(cover_path) if cover_path else ""

        for inp in inputs or []:
            name_attr = ""
            try:
                name_attr = (inp.get_attribute("name") or "").lower()
            except Exception:  # noqa: BLE001
                pass
            selector = (
                f'input[type="file"][name="{name_attr}"]'
                if name_attr
                else 'input[type="file"]'
            )
            # Discriminate by name attribute. Unknown / unmatched slots are
            # SKIPPED — never guessed with the resume. Real ATS forms include
            # portfolio & references file inputs that have no candidate-
            # profile source; stapling resume.pdf into those was the S20
            # collision bug.
            #
            # H8 fix: match discrete tokens, not substrings. `portfolio_cv_samples`
            # must NOT hit the resume branch just because it contains 'cv';
            # `coverage_letter` must NOT hit the cover-letter branch.
            target_path = ""
            target_label = ""
            if _COVER_TOKEN_RE.search(name_attr) and cover_path:
                target_path = cover_path
                target_label = "cover_letter"
            elif _RESUME_TOKEN_RE.search(name_attr) and resume_path:
                target_path = resume_path
                target_label = "resume"

            if not target_path:
                # Skip portfolio / references / unknown slots. Do NOT fall
                # back to resume — that was the collision bug.
                continue
            try:
                page.set_input_files(selector, target_path)
                log.info(
                    "apply.computer_use.file_short_circuit",
                    target=target_label,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "apply.computer_use.file_short_circuit_error",
                    target=target_label,
                    err_type=type(exc).__name__,
                )

    def _run_llm_loop(
        self,
        controller: Controller,
        ctx: ApplyContext,
        max_iter: int,
    ) -> int:
        """Drive the LLM tool-call loop. Returns the number of iterations used.

        Canned mode (L9 allowlist) walks a flat fixture list one tool call
        per turn — preserved for deterministic tests.

        Real-client mode implements the canonical Anthropic Computer Use
        agent loop
        (https://docs.anthropic.com/en/docs/agents-and-tools/computer-use):

            for turn in range(max_iter):
                response = call_llm(messages=messages, tools=...)
                messages.append(assistant tool_use blocks)
                tool_results = [
                    tool_result_with_screenshot(exec_each(block))
                    for block in response.tool_use_blocks
                ]
                if not tool_results or stop_reason == "end_turn":
                    break
                messages.append(user tool_result blocks)

        Every tool_use block on a response is executed (not just [0]).
        A screenshot is captured after each action and encoded as base64
        PNG inside the tool_result content so Claude sees the effect of
        its own action on the next turn.

        L13 status pinning is enforced by the caller — this method just
        returns an iteration count and never touches the status.
        """
        if _canned_mode_enabled():
            return self._run_canned_loop(controller, max_iter)
        return self._run_agent_loop(controller, max_iter)

    def _run_canned_loop(self, controller: Controller, max_iter: int) -> int:
        """Deterministic single-tool-per-turn walk of the canned fixture."""
        canned_script = _load_canned_script()
        iterations = 0
        for i in range(max_iter):
            iterations = i + 1
            log.info("apply.computer_use.turn_start", iter=i)
            if i >= len(canned_script):
                return i  # ran out of canned script early
            tool_call = canned_script[i]
            tool_name = tool_call.get("tool") or tool_call.get("action") or ""
            # L7: log tool NAME only — NEVER args.
            log.info("apply.computer_use.tool_called", tool=tool_name)
            args = tool_call.get("args") or {
                k: v for k, v in tool_call.items() if k not in ("tool", "action")
            }
            controller.apply_tool_call(tool_name, args)
        return iterations

    def _run_agent_loop(self, controller: Controller, max_iter: int) -> int:
        """Canonical Anthropic Computer Use agent loop with message history
        and screenshot feedback. See `_run_llm_loop` docstring for the
        pattern reference."""
        import base64

        messages: list[dict] = [
            {"role": "user", "content": "Complete the visible form. Never submit."}
        ]
        tools = [
            {
                "type": "computer_20251124",
                "name": "computer",
                "display_width_px": 1024,
                "display_height_px": 768,
            }
        ]
        iterations = 0
        for i in range(max_iter):
            iterations = i + 1
            log.info("apply.computer_use.turn_start", iter=i)

            response = call_claude_computer_use(
                system_prompt="Fill this job application form.",
                user_prompt="",  # ignored — messages carries the conversation
                tools=tools,
                max_iterations=max_iter,
                messages=messages,
            )

            tool_calls = response.get("tool_calls") or []
            stop_reason = str(response.get("stop_reason") or "")

            # Loop termination — no more tool actions requested OR explicit
            # end_turn from the model.
            if not tool_calls or stop_reason == "end_turn":
                return iterations

            # Append the assistant turn. Prefer the model's raw content
            # blocks when the transport preserved them; otherwise synthesize
            # tool_use blocks from the parsed tool_calls so the API accepts
            # the follow-up user turn.
            assistant_content = response.get("content") or []
            if not assistant_content:
                # `_parse_sdk_response` stores tool name under `tool`
                # (never `name`); fall through both to be transport-agnostic.
                assistant_content = [
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or f"toolu_{i}_{j}",
                        "name": tc.get("name") or tc.get("tool") or "computer",
                        "input": tc.get("args") or tc.get("input") or {},
                    }
                    for j, tc in enumerate(tool_calls)
                ]
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute EVERY tool_use block on this response, capture a
            # screenshot after each one, and staple them into tool_result
            # blocks for the next user turn.
            tool_results: list[dict] = []
            for j, tc in enumerate(tool_calls):
                tool_name = tc.get("tool") or tc.get("action") or tc.get("name") or ""
                # L7: log tool NAME only — NEVER args.
                log.info("apply.computer_use.tool_called", tool=tool_name)
                args = tc.get("args") or tc.get("input") or {
                    k: v
                    for k, v in tc.items()
                    if k not in ("tool", "action", "id", "type", "name")
                }
                exec_result = controller.apply_tool_call(tool_name, args)

                # Screenshot-per-action feedback. Failure to capture must
                # not crash the loop — degrade to text-only feedback.
                content_blocks: list[dict] = []
                try:
                    png = controller.screenshot()
                    if png:
                        b64 = base64.b64encode(png).decode("ascii")
                        content_blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            }
                        )
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    log.warning(
                        "apply.computer_use.screenshot_failed",
                        err_type=type(exc).__name__,
                    )
                content_blocks.append(
                    {"type": "text", "text": "ok" if exec_result.get("ok") else "error"}
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.get("id") or f"toolu_{i}_{j}",
                        "content": content_blocks,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        return iterations

    def _safe_page_url(self, page) -> str | None:
        try:
            return getattr(page, "url", None)
        except Exception:  # noqa: BLE001
            return None
