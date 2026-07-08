"""S17 auto-apply seam helper — wires the six weeks of Cluster A–F work
into `src/main.py::run_pipeline`.

The seam is HERE (not inlined into `src/main.py`) because the diff to
main.py would exceed the ~60 LOC ceiling in the spec (§Verification
before completion #15) once all six S17 responsibilities land:

    1. install_scrubber() before the first log line       (S16)
    2. configure_storage_dir() env-var injection          (S6/S3 boundary)
    3. poll_pending_reviews() once at pipeline entry      (S12)
    4. apply_to_job() per-job dispatch with soft-fail     (S2, S8)
    5. rotate() once at pipeline exit                     (S15)
    6. notify_session_expired() on SessionExpiredError    (S13)

Every external dependency is called through a `_call_*` indirection
so tests can patch the boundary WITHOUT dragging the entire cluster
into the test's import graph. This keeps the RED tests fast and their
failure signal focused.

Contracts consumed:
    * ApplyResult / ApplyContext / SessionExpiredError from `src.apply`.
    * poll_pending_reviews(gmail, now, config) -> list[ApplyEvent]  (S12).
    * apply_to_job(job_url, ctx, config) -> ApplyResult              (S2).
    * notify_session_expired(ats, user, last_run_iso, config)        (S13).
    * install_scrubber(logger=None)                                  (S16).
    * rotate(config, now=None) -> RotateResult                       (S15).
    * configure_storage_dir(config)                                  (S6/S3).

Log-event contract (per S17 spec AC#13 + S16 allowlist):
    apply.seam.enabled           — apply.enabled=true at pipeline entry
    apply.seam.disabled          — apply.enabled=false at pipeline entry
    apply.seam.error             — dispatcher soft-fail (per-job)
    apply.review.poll_started    — poll_pending_reviews returned N events
    apply.review.poll_failed     — poll_pending_reviews raised
    apply.retention.error        — rotate() raised
    apply.retention.rotated      — rotate() succeeded
    apply.session_expired        — SessionExpiredError caught + notified

Landmine discipline:
    L6:  datetime.now(timezone.utc) EVERYWHERE — never the deprecated
         zero-arg naive `utcnow` form.
    L14: apply_config is the LIVE dict from `config["apply"]` — never
         snapshotted, so mid-run mutation is observed by the dispatcher.
    L7:  install_scrubber() is called BEFORE any adapter log line — the
         install-order test proves this at every pipeline tick.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    # These imports are TYPE-ONLY to preserve the deferred-import contract
    # (spec §Code-review pass criteria: no `apply_to_job` at module top of
    # main.py). Runtime imports live inside the `_call_*` helpers below,
    # each of which is only reached when apply.enabled is truthy.
    from src.apply.types import ApplyResult

_log = structlog.get_logger("apply.seam")


# ---------------------------------------------------------------------------
# Boundary indirections — each `_call_*` is a single-line wrapper so tests
# can `monkeypatch.setattr(_seam, "_call_apply_to_job", MagicMock())` and
# steer the seam without touching the underlying apply.* modules.
# ---------------------------------------------------------------------------


def _call_install_scrubber() -> None:
    from src.apply.logging import install_scrubber

    install_scrubber()


def _call_configure_storage_dir(config: dict) -> None:
    from src.apply.credentials import configure_storage_dir

    configure_storage_dir(config)


def _call_poll_pending_reviews(*, gmail: Any, now: datetime, config: dict) -> list:
    """H2 + H3 fix: build the ReviewStore + resolve an adapter, and pass the
    WRAPPED config shape poll_pending_reviews reads (`config["apply"]`).

    ``config`` here is the INNER apply_config (unwrapped by the caller in
    initialize()). poll_pending_reviews' real signature is
    ``(gmail, store, now, config, *, adapter=None)`` and it reads
    ``config["apply"].get(...)`` — so we re-wrap before passing.
    """
    from src.apply.review import poll_pending_reviews
    from src.apply.state_store import ReviewStore

    # H1 fix: DedupDB owns the CREATE TABLE for review_pending. We touch it
    # first so the reconciled schema is in place before ReviewStore opens the
    # same DB file. Best-effort: if dedup_db_path is missing, fall back to a
    # local review-only DB path so the poller can still open a store.
    #
    # Resolve via _resolve_db_path so relative paths anchor at REPO ROOT (not
    # CWD) — prevents split-brain DB between DedupDB and ReviewStore when the
    # process is invoked from an unexpected working directory.
    from src.apply.dedup import _resolve_db_path
    db_path = _resolve_db_path(config)
    try:
        from src.apply.dedup import DedupDB
        DedupDB(db_path)
    except Exception:  # noqa: BLE001 — never-blocking; ReviewStore still runs its own CREATE.
        pass

    store = ReviewStore(db_path)

    # H4: the poller's YES branch needs an adapter to re-run the submit path.
    # MVP is greenhouse-only (per S12 D3). If greenhouse isn't importable in
    # this checkout, we still call poll — the AMBIGUOUS / NO / auto_decline
    # branches don't need the adapter, and the YES branch soft-fails
    # gracefully with adapter=None.
    adapter: Any = None
    try:
        from src.apply.adapters.greenhouse import GreenhouseAdapter
        adapter = GreenhouseAdapter()
    except Exception:  # noqa: BLE001 — poller tolerates adapter=None.
        adapter = None

    # H3: poll_pending_reviews reads config["apply"] — re-wrap the inner dict.
    wrapped = {"apply": config}
    try:
        return poll_pending_reviews(
            gmail, store, now, wrapped, adapter=adapter
        ) or []
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort.
            pass


def _call_stage_review(
    *,
    result: Any,
    ctx: Any,
    gmail: Any,
    config: dict,
) -> str:
    """B1 seam boundary for review.stage_review.

    Constructs a ReviewStore anchored on the same DB path DedupDB anchors on
    (repo-root-anchored via ``_resolve_db_path``), and delegates to
    stage_review. Returns the newly staged review_id.

    Kept as a `_call_*` indirection so tests can patch the boundary without
    dragging the entire review-loop import graph into the collection step.
    """
    from src.apply.dedup import _resolve_db_path
    from src.apply.review import stage_review
    from src.apply.state_store import ReviewStore

    db_path = _resolve_db_path(config)
    store = ReviewStore(db_path)
    try:
        return stage_review(result, ctx, gmail, store)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — teardown never-blocking
            pass


def _call_apply_to_job(*, job_url: str, ctx: Any, config: dict) -> "ApplyResult | None":
    from src.apply.dispatcher import apply_to_job

    # Shape adaptation at the seam boundary. `config` here is the INNER
    # `apply_config` dict (already unwrapped from the outer `config["apply"]`
    # in `run_for_job`), but `dispatcher.apply_to_job` — along with its
    # `_allowed_ats` / `_long_tail` helpers — reads `config.get("apply")`
    # on the WRAPPED shape. Without this re-wrap the dispatcher sees no
    # allowed_ats and every job returns `status="skipped"`. Existing seam
    # tests monkeypatch this helper, so they never observed the mismatch;
    # `test_seam_run_for_job_passes_correct_shape_to_dispatcher` guards it.
    return apply_to_job(job_url, ctx, {"apply": config})


def _call_notify_session_expired(*, ats: str, user: str, last_run_iso: str | None, config: dict) -> None:
    from src.apply.notify import notify_session_expired

    notify_session_expired(
        ats=ats,
        user=user,
        last_run_iso=last_run_iso,
        config={"apply": config},
    )


def _call_rotate(*, config: dict) -> Any:
    from src.apply.retention import rotate

    return rotate(config, now=None)


# ---------------------------------------------------------------------------
# Public API — called by src/main.py::run_pipeline
# ---------------------------------------------------------------------------


def _safe_apply_config(config: dict) -> dict:
    """H14: normalize an apply-section that might be ``None``, ``False``, or a
    non-dict scalar into a plain dict. YAML ``apply: null`` and ``apply: false``
    both slip past the S3 validator; we defensively map them to ``{}`` so no
    downstream ``.get`` blows up.
    """
    apply_cfg = config.get("apply") if isinstance(config, dict) else None
    if not isinstance(apply_cfg, dict):
        return {}
    return apply_cfg


def initialize(config: dict, gmail_client: Any | None) -> list:
    """Called ONCE per `run_pipeline` invocation BEFORE the per-job loop.

    Returns the list of ``ApplyEvent`` from the review poller (empty when
    apply.enabled=false OR the poller raised). Never raises.

    Side effects (only when apply.enabled=true):
      1. install_scrubber() — S16 PII redactor active before any log line.
      2. configure_storage_dir() — env-var injection so bare
         FernetFileBackend() calls see the config-driven storage dir.
      3. poll_pending_reviews() — S12 review-loop tick.
    """
    apply_config = _safe_apply_config(config)
    if not apply_config.get("enabled", False):
        _log.info("apply.seam.disabled")
        return []

    # 1. Scrubber MUST be installed before ANY adapter log line (L7).
    _call_install_scrubber()

    # 2. Wire the config-driven storage dir into the credentials backend.
    _call_configure_storage_dir(config)

    # 3. Poll pending reviews once — soft-fail to [] on any error.
    events: list = []
    try:
        events = _call_poll_pending_reviews(
            gmail=gmail_client,
            now=datetime.now(timezone.utc),
            config=apply_config,
        )
        _log.info("apply.review.poll_started", n=len(events))
    except Exception as exc:  # noqa: BLE001 — pipeline never-blocking
        _log.warning("apply.review.poll_failed", error=str(exc))
        events = []

    _log.info("apply.seam.enabled")
    return events


def run_for_job(
    *,
    job: dict,
    jd_text: str,
    lane: dict,
    resume_path: Path | None,
    cover_letter_path: Path | None,
    apply_config: dict,
    job_log: Any,
    resume_docx_path: Path | None = None,
    cover_letter_docx_path: Path | None = None,
    gmail_client: Any | None = None,
) -> "ApplyResult | None":
    """Called PER JOB after the HM-lookup block, before ``processed.append``.

    Returns an ``ApplyResult`` (from S2's dispatcher) or ``None``.
    NEVER RAISES — every exception path is soft-failed.

    AUDIT: `resume_path` widened to `Path | None` for the dual-output
    renderer's docx-only lane (see 05-renderer-contract-audit.md). Callers
    pass `resume_docx_path` + `cover_letter_docx_path` for the DOCX fallback
    the greenhouse + computer_use adapters consume when PDF is unavailable.

    Handles the SessionExpiredError branch by firing S13's
    ``notify_session_expired`` fast-path email and returning a
    ``skipped/session_expired`` result so the digest can surface it.

    L14 guarantee: `apply_config` is the LIVE dict; mutating
    ``config['apply']['allowed_ats']`` between run_pipeline ticks is
    observed by the dispatcher.
    """
    # H14: apply_config may be None/False when yaml maps `apply: null`.
    if not isinstance(apply_config, dict) or not apply_config.get("enabled", False):
        return None

    from src.apply.base import SessionExpiredError
    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext, ApplyResult

    # Build ApplyContext INSIDE the try — profile.load() can raise on bad
    # YAML, storage-state can fail — every failure surfaces as
    # apply.seam.error and returns None.
    try:
        profile_path = apply_config.get(
            "profile_path", "templates/candidate_profile.yaml"
        )
        profile = CandidateProfile.load(profile_path)
        # Post-review addition: seam constructs DedupDB and threads it via
        # ApplyContext. Adapters expect ctx.dedup — previously it was missing
        # from the frozen dataclass, which crashed every production apply as
        # soon as H4 delivered a real page.
        dedup = None
        try:
            from src.apply.dedup import DedupDB as _DedupDB, _resolve_db_path
            # Anchor at repo root — otherwise the ApplyContext DedupDB and the
            # review-loop DedupDB would land on different SQLite files if the
            # process's CWD isn't the repo root.
            db_path = _resolve_db_path(apply_config)
            dedup = _DedupDB(db_path)
        except Exception:  # noqa: BLE001 — seam never blocks on dedup init
            dedup = None
        ctx = ApplyContext(
            profile=profile,
            job=job,
            resume_path=resume_path,
            cover_letter_path=cover_letter_path,
            config=apply_config,
            applicant=str(apply_config.get("user", "single")),
            dry_run=bool(apply_config.get("dry_run", False)),
            mode=apply_config.get("mode", "review"),
            resume_docx_path=resume_docx_path,
            cover_letter_docx_path=cover_letter_docx_path,
            dedup=dedup,
        )
        job_url = job.get("ats_apply_url") or job.get("url", "") or ""
        result = _call_apply_to_job(job_url=job_url, ctx=ctx, config=apply_config)

        # B1: wire stage_review into the review-mode path. When the adapter
        # returns a status the review loop is meant to gate on, insert a
        # review_pending row + send the review email. Previously stage_review
        # was defined but had zero production callers → every review-required
        # application silently dead-ended.
        _STAGE_STATUSES = {"review_required", "soft_dup_warn", "captcha_escalated"}
        if (
            result is not None
            and getattr(result, "status", None) in _STAGE_STATUSES
            and gmail_client is not None
        ):
            try:
                _call_stage_review(
                    result=result,
                    ctx=ctx,
                    gmail=gmail_client,
                    config=apply_config,
                )
            except Exception as stage_exc:  # noqa: BLE001 — never-blocking
                job_log.warning(
                    "apply.review.stage_failed",
                    error=str(stage_exc),
                    status=getattr(result, "status", None),
                )
        elif (
            result is not None
            and getattr(result, "status", None) in _STAGE_STATUSES
            and gmail_client is None
        ):
            # No gmail client available (e.g. --test mode) — log and continue.
            job_log.info(
                "apply.review.stage_skipped_no_gmail_client",
                status=getattr(result, "status", None),
            )

        return result
    except SessionExpiredError as exc:
        try:
            _call_notify_session_expired(
                ats=getattr(exc, "ats", "unknown"),
                user=str(apply_config.get("user", "unknown")),
                last_run_iso=getattr(exc, "last_run_iso", None),
                config=apply_config,
            )
        except Exception as notify_exc:  # noqa: BLE001 — never-blocking
            job_log.warning("apply.seam.error", error=f"notify failed: {notify_exc}")
        job_log.info("apply.session_expired", ats=getattr(exc, "ats", "unknown"))
        return ApplyResult(
            status="skipped",
            ats=getattr(exc, "ats", None),
            reason="session_expired",
        )
    except Exception as exc:  # noqa: BLE001 — pipeline never-blocking
        job_log.warning("apply.seam.error", error=str(exc))
        return None


def finalize(config: dict) -> None:
    """Called ONCE per `run_pipeline` invocation AFTER the per-job loop.

    Handles S15 retention rotation. Never raises. No-op when
    apply.enabled=false.
    """
    apply_config = _safe_apply_config(config)
    if not apply_config.get("enabled", False):
        return
    try:
        result = _call_rotate(config=config)
        # H7 fix: RotateResult is namedtuple(deleted_traces, deleted_screenshots,
        # errors). The old `rotated_count` attr didn't exist. Emit the total
        # AND the per-kind breakdown so operators can spot skew.
        deleted_traces = getattr(result, "deleted_traces", 0) or 0
        deleted_screenshots = getattr(result, "deleted_screenshots", 0) or 0
        rotated_total = int(deleted_traces) + int(deleted_screenshots)
        _log.info(
            "apply.retention.rotated",
            rotated=rotated_total,
            deleted_traces=deleted_traces,
            deleted_screenshots=deleted_screenshots,
            errors=getattr(result, "errors", 0),
        )
    except Exception as exc:  # noqa: BLE001 — never-blocking
        _log.warning("apply.retention.error", error=str(exc))
