"""
main.py — Orchestrator for the Hiring.cafe job alert agent.

Runs the full pipeline:
  Gmail intake → Parse → Fetch JDs → Classify → Tailor → QA → PDF → Digest

Flags:
  --test      Load from test_data/sample_alert.eml instead of Gmail.
              Implies dry-run (no email send, no mark-processed).
  --dry-run   Run the full pipeline but skip sending digest and marking processed.
"""

# Ensure repo root is on sys.path when invoked as a script (`python src/main.py`).
# Module mode (`python -m src.main`) already has this on sys.path via the
# package structure. Idempotent: no-op if repo root already present.
# Must run BEFORE any `from src.apply.*` import (see run_pipeline in this file).
import os as _os_bootstrap
import sys as _sys_bootstrap
_REPO_ROOT_BOOTSTRAP = _os_bootstrap.path.dirname(
    _os_bootstrap.path.dirname(_os_bootstrap.path.abspath(__file__))
)
if _REPO_ROOT_BOOTSTRAP not in _sys_bootstrap.path:
    _sys_bootstrap.path.insert(0, _REPO_ROOT_BOOTSTRAP)
del _os_bootstrap, _sys_bootstrap, _REPO_ROOT_BOOTSTRAP

import argparse
import logging
import os
import sys
from typing import Optional
import yaml
import structlog
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from parser.email_parser import parse_alert_from_eml, parse_alert_email
from scraper.jd_fetcher import fetch_job_description
from classifier.lane_selector import classify_lane
from tailor.resume_tailor import tailor_resume
from tailor.cover_letter import write_cover_letter
from qa.checker import run_qa, auto_fix
from pdf_gen.renderer import render_resume, render_cover_letter
from gmail.digest import compose_digest
from contacts.hm_finder import find_hiring_manager

log = structlog.get_logger()

# ── Config-gate (Phase 3 auto-apply) ────────────────────────────────────────
# See .agent/one-big-feature/auto-apply-2026-07-06/03-specs/03-s3-config-gate.md
# `_validate_apply_config` runs at pipeline entry BEFORE the S17 seam. When
# apply.enabled is false, it no-ops so a malformed apply block cannot break
# the pre-Phase-3 pipeline.

_config_logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised on invalid apply: config when apply.enabled is true.

    Caught by main() and surfaced with sys.exit(1) — never leaks a stack trace
    to end-users. Never raised when apply.enabled is false (soft-fail path).
    """


# Hard-coded enum tuples — a rename in src/apply/types.py surfaces here as a
# test failure rather than a silent import shim (landmine L14 guardrail).
_ALLOWED_ATS = ("greenhouse", "lever", "ashby", "workday", "icims", "computer_use")
_MODE_VALUES = ("review", "auto")
_LONG_TAIL_VALUES = ("none", "computer_use")
_CAPTCHA_ACTION_VALUES = ("escalate", "skip")
_CAPTCHA_TRANSPORT_VALUES = ("browserbase", "local")

_APPLY_TOP_LEVEL_KEYS = frozenset({
    "enabled", "mode", "allowed_ats", "long_tail", "dry_run",
    "timeout_seconds", "navigation_retries", "rate_limit_per_ats_per_day",
    "review_timeout_hours", "review_reping_hours", "retention_days",
    "screenshot_dir", "trace_dir", "storage_state_dir", "dedup_db_path",
    "captcha_action", "captcha_transport", "profile_path",
    "gmail_label_prefix", "fast_path_recipient", "browserbase",
})
_APPLY_BROWSERBASE_KEYS = frozenset({"enabled", "solve_captchas", "proxies", "block_ads"})

# Path keys treated as DIRECTORIES (mkdir target). `dedup_db_path` is a FILE
# so its parent is what needs to be writable / created.
_PATH_DIR_KEYS = ("screenshot_dir", "trace_dir", "storage_state_dir")


def _writable_ancestor(path: Path) -> Path:
    """Walk `path` upward until an existing filesystem node is found —
    that node is the writability target. Pure filesystem inspection; no side
    effects (no mkdir), so a validation failure downstream cannot leak a
    partial-state dir tree onto disk."""
    check = path
    while not check.exists() and check.parent != check:
        check = check.parent
    return check


def _dedup_db_writability_target(raw: "str | os.PathLike[str]") -> Optional[Path]:
    """Return the parent directory to writability-check + mkdir for the
    dedup_db_path config value, or ``None`` to skip.

    Consistency fix (sibling of PR #5): naive ``Path(raw).parent`` is
    CWD-relative — so main.py's writability precheck could log
    ``dedup DB writable at ./state/…`` while ``src/apply/dedup.py`` silently
    opens ``<repo>/state/…``. Precheck was meaningless when CWDs differed.

    This routes through the SAME ``_anchor_at_repo_root`` helper dedup.py
    uses (see ``src/apply/dedup.py``), so the precheck targets the exact
    filesystem node the DB actually opens against.

    SQLite non-filesystem specs — ``":memory:"`` and ``"file:..."`` URIs —
    have nothing to check on disk; ``None`` signals "skip both the writability
    check and the mkdir." Matches the passthrough in dedup._is_sqlite_special_path.
    """
    # Lazy-import to keep the top of main.py free of apply-side dependencies
    # and to preserve the pre-Phase-3 pipeline path (no apply imports pulled
    # when apply.enabled=false — this function only fires from inside the
    # apply.enabled=true branch of _validate_apply_config).
    from apply.dedup import _anchor_at_repo_root, _is_sqlite_special_path

    if _is_sqlite_special_path(os.fspath(raw)):
        return None
    return _anchor_at_repo_root(raw).parent


def _validate_apply_config(config: dict) -> None:
    """Validate the `apply:` block. No-op when `apply.enabled` is false.

    Pure inspection — never mutates `config`. On any validation failure raises
    ConfigError WITHOUT creating any directories (atomicity: all-or-nothing).
    Directories are materialized only after every check passes.
    """
    apply_cfg = config.get("apply", {})
    if not isinstance(apply_cfg, dict):
        # Soft-fail: without a mapping we can't inspect `enabled`; treat as OFF.
        # A dict with enabled=true will be validated fully below.
        return
    if not apply_cfg.get("enabled", False):
        return  # soft-fail — pre-Phase-3 pipeline unaffected by malformed block

    # ── Unknown-key rejection ───────────────────────────────────────────────
    unknown = set(apply_cfg) - _APPLY_TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"apply: unknown key: {sorted(unknown)[0]}")

    # ── Required-key presence ───────────────────────────────────────────────
    missing = _APPLY_TOP_LEVEL_KEYS - set(apply_cfg)
    if missing:
        raise ConfigError(f"apply: missing required key: {sorted(missing)[0]}")

    # ── Bool types ──────────────────────────────────────────────────────────
    for bkey in ("enabled", "dry_run"):
        if not isinstance(apply_cfg[bkey], bool):
            raise ConfigError(
                f"apply.{bkey}: must be a bool, got {type(apply_cfg[bkey]).__name__}"
            )

    # ── Enum values ─────────────────────────────────────────────────────────
    mode = apply_cfg["mode"]
    if mode not in _MODE_VALUES:
        raise ConfigError(f"apply.mode: must be one of {_MODE_VALUES}, got {mode!r}")

    long_tail = apply_cfg["long_tail"]
    if long_tail not in _LONG_TAIL_VALUES:
        raise ConfigError(
            f"apply.long_tail: must be one of {_LONG_TAIL_VALUES}, got {long_tail!r}"
        )

    captcha_action = apply_cfg["captcha_action"]
    if captcha_action not in _CAPTCHA_ACTION_VALUES:
        raise ConfigError(
            f"apply.captcha_action: must be one of {_CAPTCHA_ACTION_VALUES}, got {captcha_action!r}"
        )

    captcha_transport = apply_cfg["captcha_transport"]
    if captcha_transport not in _CAPTCHA_TRANSPORT_VALUES:
        raise ConfigError(
            f"apply.captcha_transport: must be one of {_CAPTCHA_TRANSPORT_VALUES}, got {captcha_transport!r}"
        )

    # ── allowed_ats: non-empty list of known ATSes ──────────────────────────
    allowed = apply_cfg["allowed_ats"]
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("apply.allowed_ats: must be a non-empty list")
    for ats in allowed:
        if ats not in _ALLOWED_ATS:
            raise ConfigError(
                f"apply.allowed_ats: unknown ATS {ats!r}, must be one of {_ALLOWED_ATS}"
            )

    # ── Integer range checks ────────────────────────────────────────────────
    rate = apply_cfg["rate_limit_per_ats_per_day"]
    if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0 or rate > 100:
        raise ConfigError(
            f"apply.rate_limit_per_ats_per_day: must be int in (0, 100], got {rate!r}"
        )

    timeout_seconds = apply_cfg["timeout_seconds"]
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ConfigError(
            f"apply.timeout_seconds: must be a positive int, got {timeout_seconds!r}"
        )

    nav_retries = apply_cfg["navigation_retries"]
    if not isinstance(nav_retries, int) or isinstance(nav_retries, bool) or nav_retries < 0:
        raise ConfigError(
            f"apply.navigation_retries: must be a non-negative int, got {nav_retries!r}"
        )

    retention_days = apply_cfg["retention_days"]
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days <= 0:
        raise ConfigError(
            f"apply.retention_days: must be a positive int, got {retention_days!r}"
        )

    reping = apply_cfg["review_reping_hours"]
    review_timeout = apply_cfg["review_timeout_hours"]
    if not isinstance(reping, int) or isinstance(reping, bool) or reping <= 0:
        raise ConfigError(
            f"apply.review_reping_hours: must be a positive int, got {reping!r}"
        )
    if not isinstance(review_timeout, int) or isinstance(review_timeout, bool) or review_timeout <= 0:
        raise ConfigError(
            f"apply.review_timeout_hours: must be a positive int, got {review_timeout!r}"
        )
    if reping >= review_timeout:
        raise ConfigError(
            f"apply.review_reping_hours ({reping}) must be < review_timeout_hours ({review_timeout})"
        )

    # ── String types ────────────────────────────────────────────────────────
    label_prefix = apply_cfg["gmail_label_prefix"]
    if not isinstance(label_prefix, str) or not label_prefix:
        raise ConfigError("apply.gmail_label_prefix: must be a non-empty string")

    # ── fast_path_recipient env: prefix (L9-shaped allowlist) ──────────────
    fpr = apply_cfg["fast_path_recipient"]
    if not isinstance(fpr, str) or not fpr:
        raise ConfigError("apply.fast_path_recipient: must be a non-empty string")
    if fpr.startswith("env:"):
        env_name = fpr[len("env:"):]
        if not env_name:
            raise ConfigError(
                "apply.fast_path_recipient: 'env:' prefix requires a variable name"
            )
        if env_name not in os.environ:
            raise ConfigError(
                f"apply.fast_path_recipient: env var {env_name!r} is unset"
            )

    # ── browserbase sub-block ───────────────────────────────────────────────
    bb = apply_cfg["browserbase"]
    if not isinstance(bb, dict):
        raise ConfigError("apply.browserbase: must be a mapping")
    bb_unknown = set(bb) - _APPLY_BROWSERBASE_KEYS
    if bb_unknown:
        raise ConfigError(
            f"apply.browserbase: unknown key: {sorted(bb_unknown)[0]}"
        )
    bb_missing = _APPLY_BROWSERBASE_KEYS - set(bb)
    if bb_missing:
        raise ConfigError(
            f"apply.browserbase: missing required key: {sorted(bb_missing)[0]}"
        )
    for bkey in sorted(_APPLY_BROWSERBASE_KEYS):
        if not isinstance(bb[bkey], bool):
            raise ConfigError(
                f"apply.browserbase.{bkey}: must be a bool, got {type(bb[bkey]).__name__}"
            )

    # Transport wiring must be consistent.
    if captcha_transport == "browserbase" and not bb["enabled"]:
        raise ConfigError(
            "browserbase transport selected but browserbase.enabled is false"
        )

    # ── profile_path: file exists + dry-load via S1's CandidateProfile ─────
    profile_path = Path(apply_cfg["profile_path"])
    if not profile_path.is_file():
        raise ConfigError(
            f"apply.profile_path: file does not exist: {profile_path}"
        )

    # S1 stub or real impl — both expose CandidateProfile.load + ProfileValidationError.
    try:
        from apply.profile import CandidateProfile, ProfileValidationError
    except ImportError:  # pragma: no cover — pre-S1 defensive path
        CandidateProfile = None  # type: ignore[assignment]
        ProfileValidationError = Exception  # type: ignore[misc,assignment]
    if CandidateProfile is not None:
        try:
            CandidateProfile.load(profile_path)
        except ProfileValidationError as exc:
            raise ConfigError(
                f"apply.profile_path: invalid candidate profile: {exc}"
            ) from exc

    # ── Path key writability (dirs + dedup_db_path parent) ──────────────────
    for pkey in _PATH_DIR_KEYS:
        pval = Path(apply_cfg[pkey])
        anchor = _writable_ancestor(pval)
        if not os.access(anchor, os.W_OK):
            raise ConfigError(
                f"apply.{pkey}: parent path is not writable: {anchor}"
            )

    # Route the dedup_db_path writability check through the SAME
    # `_anchor_at_repo_root` helper that `src/apply/dedup.py` uses, so the
    # precheck reflects the actual open-path (repo-root anchor) instead of
    # a CWD-relative fiction. SQLite ``:memory:`` and ``file:`` URIs return
    # None (nothing to check on disk).
    ddp_target = _dedup_db_writability_target(apply_cfg["dedup_db_path"])
    if ddp_target is not None:
        ddp_anchor = _writable_ancestor(ddp_target)
        if not os.access(ddp_anchor, os.W_OK):
            raise ConfigError(
                f"apply.dedup_db_path: parent path is not writable: {ddp_anchor}"
            )

    # ── All validation passed — NOW emit warnings + create dirs ────────────
    if long_tail == "computer_use":
        # Ben's opt-in default OFF is expressed by the YAML default of `none`;
        # a live `computer_use` selection is a loud signal.
        _config_logger.warning("apply.long_tail.computer_use.enabled")

    for pkey in _PATH_DIR_KEYS:
        Path(apply_cfg[pkey]).mkdir(parents=True, exist_ok=True)
    # Same anchored target for mkdir — never mkdir a CWD-relative sibling of
    # the DB dedup.py actually opens against. None → SQLite special path,
    # nothing to mkdir.
    if ddp_target is not None:
        ddp_target.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def load_project_bank() -> list[dict]:
    with open(ROOT / "templates" / "project_bank.yaml") as f:
        data = yaml.safe_load(f)
    return data.get("projects", [])


def _build_attachments(processed: list[dict]) -> list[Path]:
    """Flatten per-job (resume_pdf, resume_docx, cover_letter_pdf,
    cover_letter_docx) into a single deduped attachment list.

    Resilient to:
      - PDF fallback: renderer returns None in the pdf slot when no
        LibreOffice/docx2pdf is installed — those Nones are filtered out, so
        only real files are attached and the digest body can honestly say
        "DOCX only" instead of falsely claiming a PDF + DOCX pair exists.
      - Partial-rollout dicts: missing keys (e.g. 'resume_docx' from a stale
        producer) are tolerated via .get() instead of raising KeyError.
      - Duplicate paths (e.g. older callers that returned the same docx in
        both pdf and docx slots) are deduped so attachments don't double up.
    """
    keys = (
        "resume_pdf",
        "resume_docx",
        "cover_letter_pdf",
        "cover_letter_docx",
    )
    attachments: list[Path] = []
    seen: set[str] = set()
    for p in processed:
        for k in keys:
            path = p.get(k)
            if path is None:
                continue
            # Normalise to Path + canonical string key for dedup.
            path_obj = Path(path)
            key = str(path_obj)
            if key in seen:
                continue
            seen.add(key)
            attachments.append(path_obj)
    return attachments


def run_pipeline(
    jobs: list[dict],
    config: dict,
    project_bank: list[dict],
    today: str,
    output_dir: Path,
    dry_run: bool = False,
    gmail_client=None,
) -> tuple[list[dict], list[dict], list]:
    """
    Process a list of parsed job dicts through the full pipeline.
    Returns (processed, skipped, apply_events).

    `apply_events` is the list of `ApplyEvent` items returned by the S12
    review poller (S17 seam). Empty list when `apply.enabled` is false or
    the poller failed. Callers may pass through as
    `compose_digest(processed, skipped, apply_events=apply_events)` for
    the S14 rollup.
    """
    # ── S3 config-gate: validate apply: block before any adapter code runs.
    # No-op when apply.enabled is false (soft-fail); on invalid config raises
    # ConfigError caught by main(). MUST run BEFORE the S17 auto-apply seam.
    _validate_apply_config(config)

    # ── S17 auto-apply seam: initialize once per run_pipeline invocation.
    # Called unconditionally (even when apply.enabled=false) so the S16 PII
    # scrubber (M9) is guaranteed active before any log line — including
    # contacts/hm_finder's raw-LLM-output warnings, which run regardless of
    # apply.enabled. See _seam.initialize() for the enabled-gated steps.
    # Threads apply.storage_state_dir into the S6 credentials backend, and
    # runs the S12 review poller once for the 24h/72h state machine.
    # Live-config guarantee (L14): apply_config is a REFERENCE, not a copy.
    from src.apply import _seam as _apply_seam
    # SG4 (Phase 3 xhigh iter-1): use setdefault so the SAME dict is threaded
    # through to the seam. `config.get('apply', {})` returns a fresh orphan
    # dict when 'apply' is missing, so any downstream mutation (including
    # a prior dry_run ratchet) would be lost. setdefault ensures the config
    # carries a live reference.
    apply_config = config.setdefault("apply", {})
    if not isinstance(apply_config, dict):
        # Defensive: `apply: null` / non-dict scalar. Reset to empty dict
        # on the outer config so the seam's _safe_apply_config sees it too.
        apply_config = {}
        config["apply"] = apply_config
    # SB2 (Phase 3 xhigh iter-1): DO NOT mutate `apply_config['dry_run']` here.
    # The pre-fix ratchet (`apply_config['dry_run'] = True`) was a one-way
    # flag that persisted for the lifetime of the config dict — a long-lived
    # process invoked once with `--test` (or any dry_run=True call) would
    # silently stay in dry_run forever on every subsequent call. Instead we
    # thread `dry_run` as an explicit kwarg to run_for_job; the seam OR's it
    # with the config-supplied value.
    # I2-B1: thread per-call dry_run into initialize so the review-loop
    # YES branch (execute_confirmed_submit → _AutoModeCtx) honors CLI
    # --dry-run even when config's apply.dry_run is false.
    apply_events = _apply_seam.initialize(config, gmail_client, dry_run=dry_run)

    processed = []
    skipped = []

    for i, job in enumerate(jobs):
        job_log = log.bind(job_index=i, title=job["title"], company=job["company"])
        job_log.info("step.process_job", status="starting")

        try:
            # ── Fetch JD ─────────────────────────────────────────────────────
            jd_result = fetch_job_description(
                url=job["url"],
                timeout=config["scraper"]["timeout_seconds"],
                min_length=config["scraper"]["min_jd_length"],
                job_title=job.get("title", ""),
                company=job.get("company", ""),
            )
            if jd_result is None:
                job_log.warning("step.fetch_jd", status="skipped", reason="JD retrieval failed")
                skipped.append({**job, "reason": "JD retrieval failed"})
                continue
            jd = jd_result.text
            # Surface ATS metadata onto the job dict for Phase 3 auto-apply.
            # Falls back to None when the JD came from a non-ATS source
            # (e.g. pure hiring.cafe or a company careers page).
            job["ats_apply_url"] = jd_result.ats_apply_url
            job["ats"] = jd_result.ats
            job_log.info(
                "step.fetch_jd",
                status="success",
                jd_length=len(jd),
                ats=jd_result.ats,
                ats_apply_url=jd_result.ats_apply_url,
            )

            # ── Classify lane ─────────────────────────────────────────────────
            lane = classify_lane(jd_text=jd, lanes_config=config["lanes"])
            job_log.info("step.classify_lane", lane=lane["name"])

            # ── Tailor resume ─────────────────────────────────────────────────
            tailored_resume = tailor_resume(
                jd_text=jd,
                lane=lane,
                project_bank=project_bank,
                config=config["resume"],
            )

            confidence = tailored_resume.get("confidence_score", 100)
            min_confidence = config["resume"].get("min_confidence_score", 30)
            if confidence < min_confidence:
                job_log.warning(
                    "step.tailor_resume",
                    status="skipped_low_confidence",
                    confidence=confidence,
                    threshold=min_confidence,
                )
                skipped.append({
                    **job,
                    "reason": f"Poor fit — confidence {confidence}/100 (threshold {min_confidence})",
                })
                continue

            # ── Write cover letter ────────────────────────────────────────────
            cover_letter = write_cover_letter(
                jd_text=jd,
                job=job,
                lane=lane,
                project_bank=project_bank,
                config=config["cover_letter"],
            )

            # ── QA + auto-fix loop ────────────────────────────────────────────
            qa_passed = False
            for attempt in range(config["qa"]["max_retries"] + 1):
                qa_result = run_qa(
                    tailored_resume=tailored_resume,
                    cover_letter=cover_letter,
                    jd_text=jd,
                    lane=lane,
                    config=config,
                )
                if qa_result["pass"]:
                    qa_passed = True
                    break

                job_log.warning(
                    "step.qa",
                    attempt=attempt + 1,
                    errors=qa_result["errors"],
                )

                if attempt < config["qa"]["max_retries"]:
                    tailored_resume, cover_letter = auto_fix(
                        tailored_resume=tailored_resume,
                        cover_letter=cover_letter,
                        issues=qa_result["errors"],
                        jd_text=jd,
                        lane=lane,
                        project_bank=project_bank,
                    )

            if not qa_passed:
                job_log.error("step.qa", status="failed_after_retries")
                skipped.append({**job, "reason": "QA failed after retries"})
                continue

            # ── Render DOCX + PDF ─────────────────────────────────────────────
            # DOCX is always produced; PDF is Optional (None when no
            # LibreOffice/docx2pdf is installed). Downstream code (digest body
            # + attachments) handles the None case explicitly.
            output_dir.mkdir(parents=True, exist_ok=True)
            resume_pdf, resume_docx = render_resume(
                tailored_resume=tailored_resume,
                lane=lane,
                job=job,
                date_str=today,
                output_dir=output_dir,
            )
            cl_pdf, cl_docx = render_cover_letter(
                cover_letter=cover_letter,
                job=job,
                date_str=today,
                output_dir=output_dir,
            )
            job_log.info(
                "step.render_documents",
                resume_pdf=str(resume_pdf) if resume_pdf else None,
                resume_docx=str(resume_docx),
                cover_letter_pdf=str(cl_pdf) if cl_pdf else None,
                cover_letter_docx=str(cl_docx),
            )

            # ── Hiring manager lookup ──────────────────────────────────────
            hm_info = None
            contacts_config = config.get("contacts", {})
            if contacts_config.get("enabled", False):
                try:
                    hm_info = find_hiring_manager(
                        job=job,
                        jd_text=jd,
                        lane=lane["label"],
                        config=contacts_config,
                    )
                    if hm_info:
                        job_log.info("step.hm_lookup", name=hm_info["name"],
                                     confidence=hm_info["confidence"])
                    else:
                        job_log.info("step.hm_lookup", status="not_found")
                except Exception as exc:
                    job_log.warning("step.hm_lookup", status="error", error=str(exc))

            # ── S17 auto-apply seam (per-job) ───────────────────────────────
            # Runs deferred inside `_apply_seam.run_for_job` — never raises;
            # returns ApplyResult or None. `apply_result` is stapled onto the
            # processed[] dict for the S14 digest rollup + downstream shape
            # stability. When apply.enabled is false, run_for_job returns
            # None immediately without importing the dispatcher stack.
            apply_result = _apply_seam.run_for_job(
                job=job,
                jd_text=jd,
                lane=lane,
                resume_path=resume_pdf,
                cover_letter_path=cl_pdf,
                resume_docx_path=resume_docx,
                cover_letter_docx_path=cl_docx,
                apply_config=apply_config,
                job_log=job_log,
                gmail_client=gmail_client,
                # SB2: explicit per-call dry_run flag — the seam OR's it with
                # apply_config.get('dry_run', False). This replaces the pre-fix
                # `apply_config['dry_run'] = True` one-way ratchet.
                dry_run=dry_run,
            )

            processed.append({
                **job,
                "lane": lane["label"],
                "resume_pdf": resume_pdf,
                "resume_docx": resume_docx,
                "cover_letter_pdf": cl_pdf,
                "cover_letter_docx": cl_docx,
                "hiring_manager": hm_info,
                "apply_result": apply_result,
            })

        except Exception as exc:
            job_log.error("step.process_job", status="error", error=str(exc), exc_info=True)
            skipped.append({**job, "reason": f"Unexpected error: {exc}"})

    # ── S17 auto-apply seam: finalize once per run_pipeline (retention).
    # Runs S15's `rotate(config)` inside its own try/except so a filesystem
    # error can never abort the pipeline. No-op when apply.enabled is false.
    _apply_seam.finalize(config)

    return processed, skipped, apply_events


def main() -> None:
    parser = argparse.ArgumentParser(description="Hiring.cafe job alert agent")
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Load from test_data/sample_alert.eml instead of Gmail. "
            "Skips Gmail auth, digest send, and mark-processed."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip sending digest and marking email processed.",
    )
    args = parser.parse_args()

    config = load_config()
    project_bank = load_project_bank()
    today = date.today().isoformat()

    # ── Test mode ─────────────────────────────────────────────────────────────
    if args.test:
        eml_path = ROOT / "test_data" / "sample_alert.eml"
        output_dir = ROOT / "test_data" / "output" / today

        log.info("pipeline.test_mode", eml=str(eml_path), output_dir=str(output_dir))
        jobs = parse_alert_from_eml(eml_path, max_jobs=config["jobs"]["max_per_run"])
        if not jobs:
            log.error("pipeline.test_mode", status="no_jobs_parsed")
            sys.exit(1)
        log.info("pipeline.test_mode", job_count=len(jobs))

        # H13 fix: pass a gmail_client even in --test mode. The seam's
        # poll_pending_reviews needs a client (or a stub) — passing
        # gmail_client=None would crash when the seam calls gmail.search().
        # A None-safe stub keeps --test hermetic (no real Gmail auth).
        class _NoopGmailClient:
            def search(self, query):
                return []
            def get_or_create_label(self, name):
                return f"stub:{name}"
            def send_with_labels(self, subject, body, to, labels, attachments):
                return ("stub_msg_id", "stub_thread_id")
            def apply_label(self, msg_id, label_id):
                return None
            def remove_label(self, msg_id, label_id):
                return None
            def reply_to_thread(self, thread_id, body):
                return None
        try:
            processed, skipped, apply_events = run_pipeline(
                jobs=jobs,
                config=config,
                project_bank=project_bank,
                today=today,
                output_dir=output_dir,
                dry_run=True,
                gmail_client=_NoopGmailClient(),
            )
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"\n{'='*60}")
        print(f"TEST MODE COMPLETE  ({today})")
        print(f"{'='*60}")
        print(f"Processed : {len(processed)}")
        for p in processed:
            print(f"\n  • {p['title']} @ {p['company']}  [{p['lane']}]")

            # Resume: PDF is Optional. None → fallback mode (no PDF converter).
            if p["resume_pdf"] is None:
                print(f"    Resume (DOCX, no PDF converter) : {p['resume_docx']}")
            else:
                print(f"    Resume PDF                       : {p['resume_pdf']}")
                print(f"    Resume DOCX                      : {p['resume_docx']}")

            # Cover letter: same Optional-pdf handling.
            if p["cover_letter_pdf"] is None:
                print(f"    Cover Letter (DOCX, no PDF converter) : {p['cover_letter_docx']}")
            else:
                print(f"    Cover Letter PDF                       : {p['cover_letter_pdf']}")
                print(f"    Cover Letter DOCX                      : {p['cover_letter_docx']}")
            hm = p.get("hiring_manager")
            if hm:
                print(f"    Hiring Manager       : {hm['name']} — {hm.get('title', 'N/A')} ({hm['confidence']})")
                if hm.get("linkedin_url"):
                    print(f"    LinkedIn             : {hm['linkedin_url']}")
                if hm.get("outreach_note"):
                    print(f"    Outreach Note        : {hm['outreach_note']}")
        print(f"\nSkipped   : {len(skipped)}")
        for s in skipped:
            print(f"  • {s['title']} @ {s['company']}  — {s['reason']}")
        print()
        return

    # ── Production / dry-run mode ─────────────────────────────────────────────
    from gmail.client import AuthError, GmailClient

    # SE5 (Phase 3 xhigh iter-1): catch AuthError from GmailClient() and exit
    # non-zero with a grep-able signal. Pre-fix: an expired-token AuthError
    # (raised by the B4 headless guard) propagated as an uncaught traceback
    # under a cron entrypoint — no structured event, no clear exit code,
    # operator only sees a stack trace in stderr.
    # I2-B2 (Phase 3 xhigh iter-2): also catch google.auth RefreshError.
    # SE5's original scope missed the "expired refresh grant" class — 60-day
    # Google inactivity, security event, or scope change all raise
    # RefreshError from creds.refresh(Request()), which the AuthError-only
    # catch let propagate as an uncaught traceback (the exact SE5 failure
    # mode).
    from google.auth.exceptions import RefreshError as _GoogleRefreshError
    try:
        gmail = GmailClient()
    except (AuthError, _GoogleRefreshError) as exc:
        # I2-B9: SD1 pattern — log exc_type only. print() still surfaces
        # str(exc) to stderr for operator diagnosis; only the structured
        # log line drops the payload-carrying string.
        log.error("gmail.auth_required", exc_type=type(exc).__name__)
        print(f"gmail auth required: {exc}", file=sys.stderr)
        sys.exit(2)
    log.info("step.gmail_intake", status="starting")

    alert = gmail.find_unprocessed_alert(
        sender=config["gmail"]["alert_sender"],
        subject_contains=config["gmail"]["alert_subject_contains"],
        processed_label=config["gmail"]["processed_label"],
    )

    if alert is None:
        log.info("step.gmail_intake", status="no_new_alerts")
        return

    log.info("step.gmail_intake", status="found_alert", message_id=alert["id"])

    jobs = parse_alert_email(
        html_body=alert["html"],
        text_body=alert.get("text", ""),
        max_jobs=config["jobs"]["max_per_run"],
    )
    log.info("step.parse_jobs", job_count=len(jobs))

    if not jobs:
        log.warning("step.parse_jobs", status="no_jobs_found")
        if not args.dry_run:
            gmail.mark_processed(alert["id"], config["gmail"]["processed_label"])
        return

    output_dir = ROOT / "output" / today
    try:
        processed, skipped, apply_events = run_pipeline(
            jobs=jobs,
            config=config,
            project_bank=project_bank,
            today=today,
            output_dir=output_dir,
            dry_run=args.dry_run,
            gmail_client=gmail,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        recipient = os.getenv("MY_EMAIL")
        if not recipient:
            log.error("step.send_digest", status="aborted", reason="MY_EMAIL not set")
        else:
            subject = config["gmail"]["digest_subject_template"].format(date=today)
            # S17 seam: when apply.enabled=true, compose_digest gets the
            # S12 review-poller output via apply_events kwarg (S14 rollup).
            # S14's contract: apply_events=None -> legacy str; any list
            # (even []) -> DigestPayload. We MUST pass the list (even empty)
            # when apply is enabled so the S14 extension surface stays live.
            # AUDIT: use _build_attachments() to filter None (docx-only lane)
            # + dedup, then hand the same list to compose_digest for the
            # PDF/DOCX detection note AND to gmail.send_digest for real send.
            attachments = _build_attachments(processed)

            _apply_on = bool(config.get("apply", {}).get("enabled", False))
            digest_output = compose_digest(
                processed=processed,
                skipped=skipped,
                attachments=attachments,
                apply_events=apply_events if _apply_on else None,
            )
            # S14 returns DigestPayload (namedtuple: body, attachments) when
            # apply_events is a list; a plain str otherwise. Normalize to
            # (body, extra_attachments).
            if isinstance(digest_output, str):
                body = digest_output
                extra_attachments: list = []
            else:
                body = digest_output.body
                extra_attachments = list(digest_output.attachments or [])
            # S14 review-required rows attach a confirmation screenshot;
            # append those AFTER the resume/cover-letter pairs so digest
            # ordering stays predictable.
            attachments.extend(extra_attachments)
            try:
                gmail.send_digest(
                    to=recipient,
                    subject=subject,
                    body_text=body,
                    attachments=attachments,
                )
                log.info("step.send_digest", status="sent", to=recipient)
                gmail.mark_processed(alert["id"], config["gmail"]["processed_label"])
            except Exception as exc:
                log.error("step.send_digest", status="failed", error=str(exc))

    log.info(
        "pipeline.complete",
        processed=len(processed),
        skipped=len(skipped),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
