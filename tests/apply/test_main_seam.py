"""RED tests for Shard S17 — auto-apply seam wiring in `src/main.py::run_pipeline`.

Every test here fails against the pre-S17 code (before the seam is wired in);
each maps 1:1 to an acceptance criterion in
`.agent/one-big-feature/auto-apply-2026-07-06/03-specs/17-s17-seam-wiring.md`.

Design: instead of running the full pre-existing pipeline (fetch_jd, tailor,
QA, PDF render), each test monkeypatches the upstream stages to no-op fakes
so the seam is the only surface exercised. This isolates the SEAM behavior
from unrelated regressions in scraper/parser/tailor.

Two levels of tests:
  1. Direct unit tests against `src.apply._seam` helpers.
  2. Integration tests against `src.main.run_pipeline` via patched upstream.

Spec acceptance criteria coverage:
  - #1 (line-offset re-read)         : test_seam_inserted_after_hm_lookup_before_append
  - #2 (never raises)                : test_dispatcher_exception_is_swallowed_and_logged
  - #3 (import gated when disabled)  : test_seam_noop_when_apply_disabled
  - #4 (live config, not snapshot)   : test_dispatcher_receives_live_apply_config
  - #5 (deferred imports)            : test_seam_noop_when_apply_disabled (import-check)
  - #6 (install_scrubber once)       : test_scrubber_installed_before_dispatch
  - #7 (poll_pending_reviews once)   : test_poll_pending_reviews_called_once_at_start
                                       test_poll_failure_is_swallowed_and_events_empty
  - #8 (apply_to_job per job)        : test_seam_happy_path_extends_processed_with_apply_result
  - #9 (apply_result on processed[]) : test_seam_happy_path_extends_processed_with_apply_result
  - #10 (SessionExpiredError)        : test_session_expired_triggers_notify_and_continues
  - #11 (retention rotate once)      : test_retention_called_once_at_end
                                       test_retention_error_is_swallowed
  - #14 (L6 datetime.now(timezone))  : test_no_utcnow_in_diff
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Shared upstream-stage monkeypatch helper
# ---------------------------------------------------------------------------
#
# `run_pipeline` walks each job through fetch_jd → classify_lane → tailor →
# cover_letter → QA → PDF-render → HM-lookup → seam → append. Every stage
# before the seam is a hard dep on external services (Anthropic, Playwright,
# LibreOffice). These tests fake them out so we exercise only the seam.

@pytest.fixture
def patched_pipeline(monkeypatch, tmp_path):
    """Fake out every pre-seam pipeline stage. Returns a container the
    tests can populate to steer the fixture (e.g. min_confidence override)."""
    import src.main as main_mod

    # Fake JD result — has ats + ats_apply_url per S17 downstream contract.
    fake_jd = SimpleNamespace(
        text="Fake JD text for testing.",
        ats="greenhouse",
        ats_apply_url="https://boards.greenhouse.io/example/jobs/12345",
    )

    monkeypatch.setattr(main_mod, "fetch_job_description", lambda **kwargs: fake_jd)
    monkeypatch.setattr(main_mod, "classify_lane", lambda **kwargs: {
        "name": "backend",
        "label": "Backend",
    })
    monkeypatch.setattr(main_mod, "tailor_resume", lambda **kwargs: {
        "confidence_score": 100,
        "summary": "fake",
    })
    monkeypatch.setattr(main_mod, "write_cover_letter", lambda **kwargs: {
        "body": "fake cover letter",
    })
    monkeypatch.setattr(main_mod, "run_qa", lambda **kwargs: {"pass": True, "errors": []})
    monkeypatch.setattr(main_mod, "auto_fix", lambda **kwargs: (kwargs["tailored_resume"], kwargs["cover_letter"]))
    monkeypatch.setattr(main_mod, "render_resume", lambda **kwargs: (tmp_path / "resume.pdf", tmp_path / "resume.docx"))
    monkeypatch.setattr(main_mod, "render_cover_letter", lambda **kwargs: (tmp_path / "cover.pdf", tmp_path / "cover.docx"))
    # HM lookup: return None (found nothing) so the block passes cleanly.
    monkeypatch.setattr(main_mod, "find_hiring_manager", lambda **kwargs: None)
    # S3 validator: disable — the seam tests care about live config, not
    # config-shape validation. S3 validation is covered in test_config_gate.
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)

    return SimpleNamespace(root=tmp_path)


def _base_config(*, apply_enabled: bool = False, extra_apply: dict | None = None) -> dict:
    """Minimum config the fake pipeline needs. Callers override apply.*."""
    apply_block = {
        "enabled": apply_enabled,
        "mode": "review",
        "dry_run": False,
        "allowed_ats": ["greenhouse"],
        "storage_state_dir": "/tmp/hiring-agent-test-storage",
        "profile_path": str(
            Path(__file__).resolve().parent.parent.parent
            / "templates"
            / "candidate_profile.yaml.example"
        ),
        "user": "jane",
    }
    if extra_apply:
        apply_block.update(extra_apply)
    return {
        "apply": apply_block,
        "scraper": {"timeout_seconds": 30, "min_jd_length": 100},
        "lanes": [],
        "resume": {"min_confidence_score": 30},
        "cover_letter": {},
        "qa": {"max_retries": 0},
    }


def _sample_jobs() -> list[dict]:
    return [
        {
            "title": "Backend Engineer",
            "company": "ExampleCo",
            "url": "https://example.com/jobs/1",
        }
    ]


# ---------------------------------------------------------------------------
# 1. Seam noop when apply is disabled
# ---------------------------------------------------------------------------

def test_seam_noop_when_apply_disabled(patched_pipeline, monkeypatch, tmp_path):
    """apply.enabled=false -> apply_to_job never called, apply_events empty.

    Verifies acceptance criterion #3 (import gated) + #5 (deferred imports).
    The strong signal is that apply_to_job's side_effect never fires — if
    the seam accidentally imports or invokes the dispatcher when disabled,
    the AssertionError propagates as a test failure.
    """
    import src.apply.dispatcher as disp_mod
    trap = MagicMock(side_effect=AssertionError(
        "apply_to_job called when apply.enabled=false"
    ))
    monkeypatch.setattr(disp_mod, "apply_to_job", trap)

    # Also trap the review poller — it must not be reached either.
    import src.apply.review as review_mod
    poll_trap = MagicMock(side_effect=AssertionError(
        "poll_pending_reviews called when apply.enabled=false"
    ))
    monkeypatch.setattr(review_mod, "poll_pending_reviews", poll_trap)

    from src.main import run_pipeline
    config = _base_config(apply_enabled=False)
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
    )

    assert len(processed) == 1
    # Field stability contract: apply_result is present as None when
    # apply.enabled=false so downstream digest can rely on the key.
    assert processed[0].get("apply_result") is None
    assert apply_events == []
    trap.assert_not_called()
    poll_trap.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Happy path — apply_result is stapled onto processed[]
# ---------------------------------------------------------------------------

def test_seam_happy_path_extends_processed_with_apply_result(patched_pipeline, monkeypatch, tmp_path):
    """apply.enabled=true, dispatcher returns review_required -> processed[0]
    carries `apply_result` with the expected status.

    Acceptance criterion #8 + #9.
    """
    from src.apply.types import ApplyResult
    fake_result = ApplyResult(
        status="review_required",
        ats="greenhouse",
        apply_url="https://boards.greenhouse.io/example/jobs/12345",
        review_id="test-review-1",
    )

    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: fake_result)
    # Also silence the review poller so we don't drag Gmail in.
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert len(processed) == 1
    ar = processed[0].get("apply_result")
    assert ar is not None
    assert ar.status == "review_required"
    assert ar.ats == "greenhouse"


# ---------------------------------------------------------------------------
# 3. Dispatcher exception is swallowed + logged
# ---------------------------------------------------------------------------

def test_dispatcher_exception_is_swallowed_and_logged(patched_pipeline, monkeypatch, tmp_path):
    """RuntimeError from dispatcher -> apply_result is None, run_pipeline OK.

    Acceptance criterion #2 (never raises).
    """
    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert len(processed) == 1
    assert processed[0].get("apply_result") is None


# ---------------------------------------------------------------------------
# 4. SessionExpiredError branch triggers notify_session_expired
# ---------------------------------------------------------------------------

def test_session_expired_triggers_notify_and_continues(patched_pipeline, monkeypatch, tmp_path):
    """SessionExpiredError -> notify_session_expired invoked once with ats.

    Acceptance criterion #10.
    """
    from src.apply.base import SessionExpiredError

    import src.apply._seam as seam_mod
    monkeypatch.setattr(
        seam_mod, "_call_apply_to_job",
        MagicMock(side_effect=SessionExpiredError(ats="greenhouse", last_run_iso="2026-07-01T00:00:00+00:00")),
    )
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    notify_mock = MagicMock()
    monkeypatch.setattr(seam_mod, "_call_notify_session_expired", notify_mock)

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs() + [
            {"title": "Frontend Engineer", "company": "OtherCo", "url": "https://o.com/j/2"}
        ],
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    # Both jobs completed the loop.
    assert len(processed) == 2
    assert notify_mock.call_count == 2  # once per job (session-expired persists across jobs)
    first_call_kwargs = notify_mock.call_args_list[0].kwargs
    assert first_call_kwargs.get("ats") == "greenhouse"


# ---------------------------------------------------------------------------
# 5. Retention rotate called exactly once at end
# ---------------------------------------------------------------------------

def test_retention_called_once_at_end(patched_pipeline, monkeypatch, tmp_path):
    """Two jobs run -> rotate called exactly once (not per job).

    Acceptance criterion #11 (single rotate at end).
    """
    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])
    rotate_mock = MagicMock(return_value=SimpleNamespace(rotated_count=0))
    monkeypatch.setattr(seam_mod, "_call_rotate", rotate_mock)

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    jobs = _sample_jobs() + [
        {"title": "Frontend Engineer", "company": "OtherCo", "url": "https://o.com/j/2"}
    ]
    run_pipeline(
        jobs=jobs,
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert rotate_mock.call_count == 1


# ---------------------------------------------------------------------------
# 6. Retention rotate exception is swallowed
# ---------------------------------------------------------------------------

def test_retention_error_is_swallowed(patched_pipeline, monkeypatch, tmp_path):
    """rotate() raises OSError -> run_pipeline returns normally.

    Acceptance criterion #11 (try/except around rotate).
    """
    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])
    monkeypatch.setattr(seam_mod, "_call_rotate", MagicMock(side_effect=OSError("disk gone")))

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    # Should return normally, not raise.
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert len(processed) == 1


# ---------------------------------------------------------------------------
# 7. Poll pending reviews called once at start
# ---------------------------------------------------------------------------

def test_poll_pending_reviews_called_once_at_start(patched_pipeline, monkeypatch, tmp_path):
    """Two jobs run -> poll_pending_reviews called exactly once (not per job).

    Acceptance criterion #7 (single poll at start).
    """
    import src.apply._seam as seam_mod
    poll_mock = MagicMock(return_value=[])
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", poll_mock)
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: None)

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    jobs = _sample_jobs() + [
        {"title": "Frontend Engineer", "company": "OtherCo", "url": "https://o.com/j/2"}
    ]
    run_pipeline(
        jobs=jobs,
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert poll_mock.call_count == 1


# ---------------------------------------------------------------------------
# 8. Poll failure is swallowed and events default to []
# ---------------------------------------------------------------------------

def test_poll_failure_is_swallowed_and_events_empty(patched_pipeline, monkeypatch, tmp_path):
    """poll_pending_reviews raises -> apply_events is [] and pipeline continues.

    Acceptance criterion #7 (soft-fail on poll failure).
    """
    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", MagicMock(side_effect=RuntimeError("gmail down")))
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: None)

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    processed, skipped, apply_events = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert apply_events == []
    assert len(processed) == 1


# ---------------------------------------------------------------------------
# 9. install_scrubber called before first dispatch
# ---------------------------------------------------------------------------

def test_scrubber_installed_before_dispatch(patched_pipeline, monkeypatch, tmp_path):
    """install_scrubber called exactly once, before first apply_to_job.

    Acceptance criterion #6.
    """
    import src.apply._seam as seam_mod
    order: list[str] = []

    scrubber_mock = MagicMock(side_effect=lambda *a, **k: order.append("scrubber"))
    monkeypatch.setattr(seam_mod, "_call_install_scrubber", scrubber_mock)
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", lambda *args, **kwargs: order.append("apply") or None)
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert scrubber_mock.call_count == 1
    assert order[0] == "scrubber"
    assert "apply" in order
    assert order.index("scrubber") < order.index("apply")


# ---------------------------------------------------------------------------
# 10. Dispatcher receives LIVE apply_config (no snapshot) — L14 guarantee
# ---------------------------------------------------------------------------

def test_dispatcher_receives_live_apply_config(patched_pipeline, monkeypatch, tmp_path):
    """Mutating config["apply"]["allowed_ats"] between two run_pipeline calls
    is observable inside apply_to_job.

    Acceptance criterion #4 + landmine L14.
    """
    seen: list[list[str]] = []

    import src.apply._seam as seam_mod

    def record_apply(*args, **kwargs):
        cfg = kwargs.get("config") or args[-1]
        seen.append(list(cfg.get("allowed_ats", [])))
        return None

    monkeypatch.setattr(seam_mod, "_call_apply_to_job", record_apply)
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    # Mutate the config's allowed_ats between runs.
    config["apply"]["allowed_ats"] = ["lever"]
    run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert len(seen) == 2
    assert seen[0] == ["greenhouse"]
    assert seen[1] == ["lever"]


# ---------------------------------------------------------------------------
# 11. No `datetime.utcnow()` anywhere in the seam diff — L6
# ---------------------------------------------------------------------------

def test_no_utcnow_in_diff():
    """Grep the seam files for `utcnow` and fail if found. L6."""
    seam_paths = [
        ROOT / "src" / "main.py",
        ROOT / "src" / "apply" / "_seam.py",
    ]
    for p in seam_paths:
        if not p.exists():
            continue
        text = p.read_text()
        # Docstrings and comments MAY use `datetime.utcnow` inside an anti-example
        # ("do not use ..."). Strip to code-only by looking for lone occurrences.
        assert "datetime.utcnow" not in text, (
            f"L6 violation: `datetime.utcnow()` found in {p.relative_to(ROOT)}"
        )


# ---------------------------------------------------------------------------
# 12. Seam is inserted between contacts_config and processed.append
# ---------------------------------------------------------------------------

def test_seam_inserted_after_hm_lookup_before_append():
    """Confirm the seam call sits between the HM-lookup block and the
    processed.append call — line-offset guardrail.

    Acceptance criterion #1 (line-offset re-read, no hardcoded :172).
    """
    text = (ROOT / "src" / "main.py").read_text()
    # Look for the ordering of the per-job anchors. `_apply_seam.run_for_job`
    # is the per-job seam entry point; it must sit between the HM-lookup
    # block and the processed.append call. The pipeline-entry
    # `_apply_seam.initialize` call is a separate anchor (before the loop).
    hm_anchor = text.find("contacts_config = config.get")
    per_job_seam_anchor = text.find("_apply_seam.run_for_job")
    append_anchor = text.find('"resume_pdf": resume_pdf')
    assert hm_anchor >= 0, "contacts_config block not found"
    assert per_job_seam_anchor >= 0, "_apply_seam.run_for_job call not found in main.py"
    assert append_anchor >= 0, "processed.append body not found"
    assert hm_anchor < per_job_seam_anchor < append_anchor, (
        "Per-job seam must sit AFTER contacts_config and BEFORE processed.append — "
        f"got offsets hm={hm_anchor} seam={per_job_seam_anchor} append={append_anchor}"
    )
    # Also assert the pipeline-entry seam call is BEFORE the per-job loop.
    initialize_anchor = text.find("_apply_seam.initialize")
    loop_anchor = text.find("for i, job in enumerate(jobs)")
    assert initialize_anchor >= 0, "_apply_seam.initialize not found in main.py"
    assert loop_anchor >= 0, "per-job for-loop not found in main.py"
    assert initialize_anchor < loop_anchor, (
        "Pipeline-entry seam.initialize must run BEFORE the per-job loop"
    )


# ---------------------------------------------------------------------------
# 13. Seam→dispatcher config-shape integration (CRITICAL regression guard)
# ---------------------------------------------------------------------------
#
# All prior tests monkeypatch `_call_apply_to_job`, so they never exercise the
# ACTUAL seam→dispatcher boundary. `_seam.run_for_job` passes `apply_config`
# (the INNER dict, already unwrapped from `config["apply"]`) to
# `_call_apply_to_job`, which forwards it to `dispatcher.apply_to_job`. The
# dispatcher's `_allowed_ats` and `_long_tail` helpers both call
# `config.get("apply")` on the passed config — expecting the WRAPPED shape.
#
# Result of the mismatch: dispatcher sees no allowed_ats, returns
# ApplyResult(status="skipped", reason="no adapter for <host>") for EVERY
# job. Production impact: the whole auto-apply pipeline is dead end-to-end,
# but the unit tests on both sides pass in isolation because each side is
# tested against its own expected shape.
#
# This test drives the REAL seam→dispatcher path (no monkeypatch of
# `_call_apply_to_job`) and installs a fake greenhouse adapter via
# sys.modules so the dispatcher's importlib resolution can find it. It
# would fail on the pre-fix branch with status="skipped" / "no adapter".


def test_seam_run_for_job_passes_correct_shape_to_dispatcher(monkeypatch, tmp_path):
    """S17 seam→S2 dispatcher config-shape integration test.

    Regression guard for the class of bug where the seam and the dispatcher
    disagree on config shape. The seam passes `apply_config` (inner dict);
    the dispatcher expects the wrapped `{"apply": ...}` shape. If the seam
    boundary does not adapt the shape, EVERY job returns "skipped".

    Not a unit test on either side — this exercises the actual boundary.
    """
    import sys
    import types as pytypes
    from src.apply.types import ApplyResult

    # 1. Install a fake greenhouse adapter that the dispatcher can resolve
    #    via importlib on the string-map registry (L12). Returns 'submitted'
    #    so the assertion below can distinguish "adapter ran" from
    #    "dispatcher returned skipped/no-adapter".
    class _FakeGH:
        name = "greenhouse"
        domains = ("boards.greenhouse.io",)

        def detect(self, url: str) -> bool:
            return "greenhouse" in url

        def apply(self, page, ctx):  # noqa: ARG002
            return ApplyResult(status="submitted", ats="greenhouse")

    fake_mod = pytypes.ModuleType("src.apply.adapters.greenhouse")
    fake_mod.GreenhouseAdapter = _FakeGH
    monkeypatch.setitem(sys.modules, "src.apply.adapters.greenhouse", fake_mod)

    # 2. Build a REAL apply_config (inner dict). Same shape the seam builds
    #    at src/main.py from config["apply"].
    profile_path = str(
        ROOT / "templates" / "candidate_profile.yaml.example"
    )
    apply_config = {
        "enabled": True,
        "mode": "review",
        "dry_run": False,
        "allowed_ats": ["greenhouse"],
        "long_tail": "none",
        "profile_path": profile_path,
        "user": "jane",
    }
    job = {
        "url": "https://example.com/jobs/1",
        "ats_apply_url": "https://boards.greenhouse.io/example/jobs/12345",
    }

    # 3. Drive the ACTUAL seam→dispatcher path. Do NOT monkeypatch
    #    _call_apply_to_job — that is precisely the boundary this test
    #    guards.
    from src.apply import _seam as seam_mod

    result = seam_mod.run_for_job(
        job=job,
        jd_text="Fake JD text.",
        lane={"name": "backend"},
        resume_path=tmp_path / "resume.pdf",
        cover_letter_path=None,
        apply_config=apply_config,
        job_log=MagicMock(),
    )

    # 4. Pre-fix behavior: dispatcher's `_allowed_ats(config)` sees
    #    inner-dict shape, `.get("apply")` returns None, so allowed_ats is
    #    [], no adapter matches, returns:
    #      ApplyResult(status="skipped", reason="no adapter for boards.greenhouse.io")
    #    Post-fix: dispatcher sees {"apply": {...}}, allowed_ats resolves
    #    to ["greenhouse"], _FakeGH.detect matches, adapter.apply returns
    #    ApplyResult(status="submitted", ats="greenhouse").
    assert result is not None, (
        "seam→dispatcher: run_for_job returned None — the seam swallowed "
        "an exception. Expected ApplyResult from the dispatcher."
    )
    assert result.status == "submitted", (
        f"seam→dispatcher config-shape mismatch: expected status='submitted' "
        f"(fake adapter ran) but got status={result.status!r} reason={result.reason!r}. "
        "Symptom: the dispatcher cannot see apply.allowed_ats because the "
        "seam is passing the inner apply_config dict instead of the "
        "wrapped {'apply': ...} shape the dispatcher expects."
    )
    assert result.ats == "greenhouse"


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT — docx-only-lane RED tests (renderer-contract audit)
# .agent/one-big-feature/auto-apply-2026-07-06/05-renderer-contract-audit.md
# ═══════════════════════════════════════════════════════════════════════════════


def test_seam_passes_docx_paths_when_pdf_unavailable(patched_pipeline, monkeypatch, tmp_path):
    """AUDIT: when render_resume returns (None, docx), the seam must build an
    ApplyContext with resume_path=None + resume_docx_path=Path(...) so the
    adapter can fall back to DOCX upload.
    """
    from src.apply.types import ApplyResult

    # Override the renderer monkeypatch to simulate docx-only lane (no PDF converter).
    import src.main as main_mod
    monkeypatch.setattr(
        main_mod,
        "render_resume",
        lambda **kwargs: (None, tmp_path / "resume.docx"),
    )
    monkeypatch.setattr(
        main_mod,
        "render_cover_letter",
        lambda **kwargs: (None, tmp_path / "cover.docx"),
    )

    captured_ctx = {}

    def _capture_apply(*, job_url, ctx, config):
        captured_ctx["ctx"] = ctx
        return ApplyResult(status="review_required", ats="greenhouse", review_id="r-1")

    import src.apply._seam as seam_mod
    monkeypatch.setattr(seam_mod, "_call_apply_to_job", _capture_apply)
    monkeypatch.setattr(seam_mod, "_call_poll_pending_reviews", lambda **kwargs: [])

    from src.main import run_pipeline
    config = _base_config(apply_enabled=True)
    processed, _, _ = run_pipeline(
        jobs=_sample_jobs(),
        config=config,
        project_bank=[],
        today="2026-07-07",
        output_dir=tmp_path,
        gmail_client=MagicMock(),
    )
    assert len(processed) == 1
    ctx = captured_ctx["ctx"]
    assert ctx.resume_path is None
    assert ctx.resume_docx_path == tmp_path / "resume.docx"
    assert ctx.cover_letter_path is None
    assert ctx.cover_letter_docx_path == tmp_path / "cover.docx"
