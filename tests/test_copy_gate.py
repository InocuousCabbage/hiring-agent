"""Tests for the copy-audit gate, the runtime employer list, and the
cover-letter prompt rules.

All names here are invented (Northwind Analytics, Bluefin Labs, Fictional Co).
No test calls the network or the real Claude API.
"""
from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIXTURE_EMPLOYERS = ["Northwind Analytics", "Fictional Co"]


@pytest.fixture
def injected_employers():
    """Inject a fixture employer list so run_qa never reads the real file.

    Tolerates the module being absent so the copy-gate tests can show RED on
    the pre-change code as an assertion failure rather than an import error.
    """
    try:
        from src.utils.employers import set_allowed_employers
    except ImportError:
        yield None
        return
    set_allowed_employers(FIXTURE_EMPLOYERS)
    try:
        yield FIXTURE_EMPLOYERS
    finally:
        set_allowed_employers(None)


def _resume(bullets=None, summary="Marketing analyst with five years in lifecycle reporting."):
    return {
        "summary": summary,
        "skills": ["s"] * 9,
        "roles": [{"index": 0, "bullets": bullets or ["Built weekly reporting for the sales team."]}],
        "keywords_integrated": ["kw"],
    }


def _run_qa(resume, paragraphs):
    from qa.checker import run_qa
    return run_qa(
        tailored_resume=resume,
        cover_letter={"paragraphs": paragraphs},
        jd_text="fake jd",
        lane={"name": "pmm"},
        config={},
    )


_CLEAN_PARAS = [
    "Your team is rebuilding its reporting stack this year.",
    "I rebuilt a reporting stack for a mid-size retailer last spring.",
]


# ── 1. em dash in a cover letter paragraph is a QA error ─────────────────────


def test_em_dash_in_cover_letter_is_copy_error(injected_employers):
    paras = ["Your team is rebuilding its reporting stack \u2014 I have done that.", _CLEAN_PARAS[1]]
    result = _run_qa(_resume(), paras)
    assert not result["pass"]
    copy_errors = [e for e in result["errors"] if e.startswith("copy check failed: EM-DASH")]
    assert copy_errors, result["errors"]
    assert "cover_letter.paragraphs[0]" in copy_errors[0]


def test_clean_cover_letter_has_no_copy_error(injected_employers):
    result = _run_qa(_resume(), list(_CLEAN_PARAS))
    assert not [e for e in result["errors"] if e.startswith("copy check failed")]
    assert result["pass"], result["errors"]


def test_copy_gate_covers_resume_summary_and_bullets(injected_employers):
    resume = _resume(
        bullets=["Shipped a dashboard \u2014 used daily.", "Ran weekly reporting."],
        summary="We don't just report numbers, we explain them.",
    )
    result = _run_qa(resume, list(_CLEAN_PARAS))
    errs = [e for e in result["errors"] if e.startswith("copy check failed")]
    assert any("EM-DASH" in e and "resume.roles[0].bullets[0]" in e for e in errs), errs
    assert any("ANTITHESIS" in e and "resume.summary" in e for e in errs), errs


def test_doc_words_do_not_suppress_the_gate(injected_employers):
    """Upstream skips lines containing 'never' or 'example' as documentation.
    Generated copy is not documentation, so the gate must not inherit that."""
    paras = ["I never miss a deadline \u2014 for example, last quarter.", _CLEAN_PARAS[1]]
    result = _run_qa(_resume(), paras)
    assert any(e.startswith("copy check failed: EM-DASH") for e in result["errors"])


# ── 6. warn-level hits do not fail QA ────────────────────────────────────────


def test_warn_level_hit_does_not_fail_qa(injected_employers):
    paras = ["Your team needs a seamless reporting stack.", _CLEAN_PARAS[1]]
    result = _run_qa(_resume(), paras)
    assert result["pass"], result["errors"]
    assert not [e for e in result["errors"] if "HYPE-ADJ" in e]


def test_scan_fields_reports_warn_hits_separately():
    from qa.copy_gate import scan_fields
    errors, warnings = scan_fields({"cover_letter.paragraphs[0]": "A seamless handoff."})
    assert errors == []
    assert [(w.rule_id, w.field) for w in warnings] == [("HYPE-ADJ", "cover_letter.paragraphs[0]")]


# ── 2. employer loader fails loud ────────────────────────────────────────────


def _write_local(tmp_path, text):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.local.yaml").write_text(text)


def test_employer_loader_empty_list_fails_loud(tmp_path):
    from src.utils.employers import EmployerListError, load_allowed_employers
    _write_local(tmp_path, "user:\n  company: Fictional Co\nemployers: []\n")
    with pytest.raises(EmployerListError) as exc:
        load_allowed_employers(tmp_path)
    assert "settings.local.yaml" in str(exc.value)
    assert "employers" in str(exc.value)


def test_employer_loader_missing_key_fails_loud(tmp_path):
    from src.utils.employers import EmployerListError, load_allowed_employers
    _write_local(tmp_path, "user:\n  company: Fictional Co\n")
    with pytest.raises(EmployerListError) as exc:
        load_allowed_employers(tmp_path)
    assert "employers" in str(exc.value)


def test_employer_loader_missing_file_fails_loud(tmp_path):
    from src.utils.employers import EmployerListError, load_allowed_employers
    (tmp_path / "config").mkdir()
    with pytest.raises(EmployerListError) as exc:
        load_allowed_employers(tmp_path)
    assert "settings.local.yaml" in str(exc.value)


def test_employer_loader_rejects_example_placeholder(tmp_path):
    from src.utils.employers import EmployerListError, load_allowed_employers
    _write_local(tmp_path, "employers:\n  - YOUR_EMPLOYER_1\n")
    with pytest.raises(EmployerListError):
        load_allowed_employers(tmp_path)


def test_employer_loader_includes_user_company(tmp_path):
    from src.utils.employers import load_allowed_employers
    _write_local(tmp_path, "user:\n  company: Fictional Co\nemployers:\n  - Northwind Analytics\n")
    assert load_allowed_employers(tmp_path) == frozenset({"northwind analytics", "fictional co"})


def test_injecting_empty_list_fails_loud():
    from src.utils.employers import EmployerListError, set_allowed_employers
    with pytest.raises(EmployerListError):
        set_allowed_employers([])


def test_placeholder_company_list_is_gone():
    import qa.checker as checker
    assert not hasattr(checker, "_ALLOWED_COMPANIES")


# ── 3. injected employer passes, invented unlisted one is flagged ────────────


def _fabrication_errors(result):
    return [e for e in result["errors"] if "fabricated company" in e]


def test_listed_employer_with_corp_suffix_passes(injected_employers):
    result = _run_qa(_resume(bullets=["Led reporting at Northwind Analytics Inc. for two years."]),
                     list(_CLEAN_PARAS))
    assert not _fabrication_errors(result), result["errors"]


def test_unlisted_employer_with_corp_suffix_is_flagged(injected_employers):
    result = _run_qa(_resume(bullets=["Built dashboards for Bluefin Labs LLC."]), list(_CLEAN_PARAS))
    assert _fabrication_errors(result), result["errors"]


def test_unlisted_org_style_name_without_legal_suffix_is_flagged(injected_employers):
    result = _run_qa(_resume(bullets=["Built dashboards for Bluefin Labs."]), list(_CLEAN_PARAS))
    assert _fabrication_errors(result), result["errors"]


@pytest.mark.parametrize("bullet", [
    "Managed Google Analytics reporting for the Growth team.",
    "Grew Qualified Pipeline By 40% In Two Quarters.",
    "Owned the CRM migration and the Weekly Revenue Review.",
])
def test_ordinary_capitalised_words_are_not_flagged(injected_employers, bullet):
    result = _run_qa(_resume(bullets=[bullet]), list(_CLEAN_PARAS))
    assert not _fabrication_errors(result), result["errors"]


# ── 4. no default close phrase in the cover letter prompt ────────────────────


def test_system_prompt_has_no_default_welcome_close():
    from tailor.cover_letter import SYSTEM_PROMPT
    low = SYSTEM_PROMPT.lower()
    for apostrophe in ("'", "\u2019"):
        assert f"i{apostrophe}d welcome a conversation" not in low
    assert "i would welcome a conversation" not in low


def test_system_prompt_limits_jd_mirroring():
    """Rule 6 used to say only 'MIRROR THE JD' with no limit."""
    from tailor.cover_letter import SYSTEM_PROMPT
    assert "MIRROR THE JD \u2014 use the job description" not in SYSTEM_PROMPT
    assert "once" in SYSTEM_PROMPT.lower()


def test_system_prompt_does_not_prescribe_a_paragraph_skeleton():
    """The output example used to assign a fixed job to each paragraph slot
    (hook, project, second project, closing), which every letter copied."""
    from tailor.cover_letter import SYSTEM_PROMPT
    low = SYSTEM_PROMPT.lower()
    assert "second paragraph" not in low
    assert "third paragraph" not in low
    assert "second project" not in low


def test_system_prompt_keeps_gap_admission_wording():
    from tailor.cover_letter import SYSTEM_PROMPT
    assert "note any mismatch inside a paragraph naturally" in SYSTEM_PROMPT


# ── 5. persistent copy errors still render and reach the digest ──────────────


_COPY_MSG = "copy check failed: EM-DASH: '\u2014' in cover_letter.paragraphs[0]"


@pytest.fixture
def copy_pipeline(monkeypatch, tmp_path):
    """Fake every pre-seam stage, modelled on tests/apply/test_main_seam.py."""
    import src.main as main_mod

    calls = SimpleNamespace(render_resume=0, render_cover_letter=0, auto_fix=0, qa_errors=[_COPY_MSG],
                            copy_errors=[{"rule_id": "EM-DASH", "matched": "\u2014",
                                          "field": "cover_letter.paragraphs[0]", "message": _COPY_MSG}])
    fake_jd = SimpleNamespace(text="Fake JD text for testing.", ats=None, ats_apply_url=None)

    def fake_qa(**kwargs):
        return {"pass": False, "errors": list(calls.qa_errors), "copy_errors": list(calls.copy_errors)}

    def fake_fix(**kwargs):
        calls.auto_fix += 1
        return kwargs["tailored_resume"], kwargs["cover_letter"]

    def fake_render_resume(**kwargs):
        calls.render_resume += 1
        return tmp_path / "resume.pdf", tmp_path / "resume.docx"

    def fake_render_cl(**kwargs):
        calls.render_cover_letter += 1
        return tmp_path / "cover.pdf", tmp_path / "cover.docx"

    monkeypatch.setattr(main_mod, "shared_browser", lambda **kw: nullcontext(None))
    monkeypatch.setattr(main_mod, "fetch_job_description", lambda **kw: fake_jd)
    monkeypatch.setattr(main_mod, "classify_lane", lambda **kw: {"name": "pmm", "label": "PMM"})
    monkeypatch.setattr(main_mod, "tailor_resume", lambda **kw: {"confidence_score": 100, "summary": "x"})
    monkeypatch.setattr(main_mod, "write_cover_letter", lambda **kw: {"paragraphs": ["a", "b"]})
    monkeypatch.setattr(main_mod, "run_qa", fake_qa)
    monkeypatch.setattr(main_mod, "auto_fix", fake_fix)
    monkeypatch.setattr(main_mod, "render_resume", fake_render_resume)
    monkeypatch.setattr(main_mod, "render_cover_letter", fake_render_cl)
    monkeypatch.setattr(main_mod, "find_hiring_manager", lambda **kw: None)
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)
    return calls


def _pipeline_config():
    return {
        "apply": {"enabled": False},
        "scraper": {"timeout_seconds": 30, "min_jd_length": 10},
        "lanes": [],
        "resume": {"min_confidence_score": 30},
        "cover_letter": {},
        "qa": {"max_retries": 2},
    }


def _jobs():
    return [{"title": "Lifecycle Analyst", "company": "Fictional Co",
             "url": "https://example.com/jobs/1", "location": "Remote"}]


def test_persistent_copy_errors_render_and_mark_digest(copy_pipeline, tmp_path):
    from src.main import compose_digest, run_pipeline
    processed, skipped, _ = run_pipeline(
        jobs=_jobs(), config=_pipeline_config(), project_bank=[], today="2026-10-06",
        output_dir=tmp_path,
    )
    assert copy_pipeline.auto_fix == 2, "auto_fix retry loop must still run"
    assert skipped == []
    assert len(processed) == 1
    assert copy_pipeline.render_resume == 1 and copy_pipeline.render_cover_letter == 1
    assert processed[0]["copy_check_failed"] == ["EM-DASH"]
    body = compose_digest(processed, skipped)
    assert "copy check failed: EM-DASH" in body


def test_non_copy_qa_failure_still_skips(copy_pipeline, tmp_path):
    from src.main import run_pipeline
    copy_pipeline.qa_errors = ["summary is empty"]
    copy_pipeline.copy_errors = []
    processed, skipped, _ = run_pipeline(
        jobs=_jobs(), config=_pipeline_config(), project_bank=[], today="2026-10-06",
        output_dir=tmp_path,
    )
    assert processed == []
    assert [s["reason"] for s in skipped] == ["QA failed after retries"]
    assert copy_pipeline.render_resume == 0


def test_mixed_copy_and_other_failure_still_skips(copy_pipeline, tmp_path):
    from src.main import run_pipeline
    copy_pipeline.qa_errors = ["summary is empty", _COPY_MSG]
    processed, skipped, _ = run_pipeline(
        jobs=_jobs(), config=_pipeline_config(), project_bank=[], today="2026-10-06",
        output_dir=tmp_path,
    )
    assert processed == []
    assert [s["reason"] for s in skipped] == ["QA failed after retries"]


def test_digest_has_no_marker_for_clean_job():
    from src.main import compose_digest
    job = {**_jobs()[0], "lane": "PMM", "hiring_manager": None}
    assert "copy check failed" not in compose_digest([job], [])
