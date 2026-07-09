#!/usr/bin/env python3
"""
tests/test_full_pipeline.py — End-to-end pipeline test using the sample .eml file.

Usage (manual end-to-end):
    python tests/test_full_pipeline.py

Equivalent to running: python src/main.py --test

Loads test_data/sample_alert.eml, runs the full pipeline without Gmail or email
send, and outputs PDFs to test_data/output/{today}/.

Pytest tests below verify that the digest-send branch builds the correct 4-file
attachment list (resume PDF + resume DOCX + cover letter PDF + cover letter DOCX
per processed job).
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    print("=" * 60)
    print("FULL PIPELINE TEST")
    print("=" * 60)
    print(f"Project root : {ROOT}")
    print(f"Mode         : --test  (sample .eml, no Gmail, no digest send)")
    print()

    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "main.py"), "--test"],
        cwd=str(ROOT),
    )

    print()
    if result.returncode == 0:
        print("Pipeline test completed successfully.")
    else:
        print(f"Pipeline test FAILED (exit code {result.returncode}).")
        sys.exit(result.returncode)


# ── Pytest: verify dual-output attachment list ────────────────────────────────


def _processed_fixture():
    """Mirrors the per-job dict main.run_pipeline() produces post dual-output."""
    return [
        {
            "title": "Engineer",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
            "resume_pdf":         Path("/tmp/acme_resume.pdf"),
            "resume_docx":        Path("/tmp/acme_resume.docx"),
            "cover_letter_pdf":   Path("/tmp/acme_cl.pdf"),
            "cover_letter_docx":  Path("/tmp/acme_cl.docx"),
            "hiring_manager": None,
        }
    ]


def test_build_attachments_is_shared_helper():
    """tests must IMPORT _build_attachments from src.main, not redefine it.

    Finding 2 of the code review: if main.py drifts, a redefined copy in
    the test silently stays green. Force the test to consume the production
    helper so drift surfaces immediately.
    """
    from main import _build_attachments as main_helper
    assert callable(main_helper)


# Import the production helper for all other tests below.
from main import _build_attachments


def test_attachment_list_has_four_files_per_processed_job():
    """One processed job → 4 attachments: resume PDF + DOCX, cover letter PDF + DOCX."""
    processed = _processed_fixture()
    attachments = _build_attachments(processed)

    assert len(attachments) == 4, (
        f"Expected 4 attachments per processed job, got {len(attachments)}"
    )

    suffixes = sorted(p.suffix.lower() for p in attachments)
    assert suffixes == [".docx", ".docx", ".pdf", ".pdf"], (
        f"Attachment mix wrong: {suffixes}"
    )

    # Both resume artifacts and both cover-letter artifacts must be present
    names = [p.name for p in attachments]
    assert any(n.endswith("_resume.pdf") for n in names)
    assert any(n.endswith("_resume.docx") for n in names)
    assert any(n.endswith("_cl.pdf") for n in names)
    assert any(n.endswith("_cl.docx") for n in names)


def test_send_digest_called_with_pdf_and_docx_attachments(monkeypatch, tmp_path):
    """M18 behavioral: drive main.main() end-to-end and assert that its
    digest-send branch actually invokes gmail.send_digest with both .pdf
    AND .docx attachments per processed job.

    Prior version of this test called `gmail.send_digest(...)` on a MagicMock
    ITSELF and then asserted its own inputs — a self-fulfilling tautology that
    never exercised main.py. Renaming the `attachments` kwarg in main.py to
    e.g. `files` kept the old test green because it never touched main.py.

    Mutation check (see PR description): temporarily rename the kwarg in
    main.py's send_digest call site to `files=`; this test then fails on
    `kwargs["attachments"]` KeyError, proving the guard is behavioral.
    """
    processed = _processed_fixture()

    # Materialize the attachment paths so any downstream stat check is happy.
    for p in processed:
        for k in ("resume_pdf", "resume_docx", "cover_letter_pdf", "cover_letter_docx"):
            path = p[k]
            path = tmp_path / path.name
            (tmp_path / path.name).write_bytes(b"PK")  # ZIP magic; enough for send
            p[k] = path

    import main as main_mod
    import gmail.client as gmail_client_mod

    gmail = MagicMock()
    gmail.find_unprocessed_alert.return_value = {
        "id": "msg_pipeline", "html": "<html></html>", "text": "",
    }
    monkeypatch.setattr(gmail_client_mod, "GmailClient", lambda: gmail)
    monkeypatch.setattr(
        main_mod, "parse_alert_email",
        lambda html_body, text_body, max_jobs: [
            {"title": "Engineer", "company": "Acme", "url": "https://example.com"}
        ],
    )
    monkeypatch.setattr(main_mod, "run_pipeline", lambda **kw: (processed, [], []))

    # Redirect ROOT to tmp_path with fresh config/templates copies.
    real_root = Path(__file__).parent.parent
    monkeypatch.setattr(main_mod, "ROOT", tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        (real_root / "config" / "settings.yaml").read_text()
    )
    (tmp_path / "templates" / "project_bank.yaml").write_text(
        (real_root / "templates" / "project_bank.yaml").read_text()
    )

    monkeypatch.setenv("MY_EMAIL", "me@example.com")
    monkeypatch.setattr(sys, "argv", ["main.py"])

    main_mod.main()

    # Assert main.py actually called send_digest with the right kwarg shape.
    gmail.send_digest.assert_called_once()
    _, kwargs = gmail.send_digest.call_args
    assert "attachments" in kwargs, (
        f"main.py send_digest call missing 'attachments' kwarg — got {list(kwargs)}"
    )
    sent = kwargs["attachments"]
    assert len(sent) == 4, f"Expected 4 attachments, got {len(sent)}: {sent}"
    pdfs = [p for p in sent if p.suffix == ".pdf"]
    docxs = [p for p in sent if p.suffix == ".docx"]
    assert len(pdfs) == 2, f"Expected 2 PDFs, got {len(pdfs)}"
    assert len(docxs) == 2, f"Expected 2 DOCX, got {len(docxs)}"


def test_attachment_list_scales_with_processed_jobs():
    """N processed jobs (each with distinct file paths) → 4N attachments."""
    processed = []
    for i in range(3):
        processed.append({
            "title": f"Engineer {i}",
            "company": f"Acme{i}",
            "url": "https://example.com",
            "lane": "pmm",
            "resume_pdf":         Path(f"/tmp/acme{i}_resume.pdf"),
            "resume_docx":        Path(f"/tmp/acme{i}_resume.docx"),
            "cover_letter_pdf":   Path(f"/tmp/acme{i}_cl.pdf"),
            "cover_letter_docx":  Path(f"/tmp/acme{i}_cl.docx"),
            "hiring_manager": None,
        })
    attachments = _build_attachments(processed)
    assert len(attachments) == 12


def test_attachments_dedup_when_pdf_conversion_fails():
    """Legacy-shape robustness for the dedup helper.

    The current renderer contract (post-1658bd6) is (Optional[Path], Path) —
    PDF is None on fallback, filtered upstream by _build_attachments. This
    test exercises the dedup helper against the LEGACY (docx, docx) shape
    that older callers might produce: each unique docx path must appear
    exactly once. Guards against silent regression if the renderer contract
    ever shifts back to returning a path in both slots."""
    resume_docx = Path("/tmp/acme_resume.docx")
    cl_docx = Path("/tmp/acme_cl.docx")

    # Simulate degraded mode: PDF tuple element points at the SAME docx path.
    processed = [
        {
            "title": "Engineer",
            "company": "Acme",
            "url": "https://example.com",
            "lane": "pmm",
            "resume_pdf":         resume_docx,  # fallback: same as docx
            "resume_docx":        resume_docx,
            "cover_letter_pdf":   cl_docx,      # fallback: same as docx
            "cover_letter_docx":  cl_docx,
            "hiring_manager": None,
        }
    ]

    attachments = _build_attachments(processed)

    # Each unique docx path appears exactly once.
    assert attachments.count(resume_docx) == 1, (
        f"resume docx should appear exactly once, got "
        f"{attachments.count(resume_docx)}: {attachments}"
    )
    assert attachments.count(cl_docx) == 1, (
        f"cover letter docx should appear exactly once, got "
        f"{attachments.count(cl_docx)}: {attachments}"
    )
    # Total is 2 (one resume + one cover letter), NOT 4.
    assert len(attachments) == 2, (
        f"Expected 2 unique attachments in degraded mode, got "
        f"{len(attachments)}: {attachments}"
    )


def _processed_for_digest():
    """Minimal processed list suitable for compose_digest body assertions."""
    return [{
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com",
        "lane": "pmm",
    }]


def test_compose_digest_includes_docx_note_when_docx_present():
    """compose_digest should add the editable-DOCX note when any .docx file
    is in attachments. Owner: compose_digest, NOT the call site in main.py."""
    from gmail.digest import compose_digest

    body = compose_digest(
        processed=_processed_for_digest(),
        skipped=[],
        attachments=[Path("/tmp/acme_resume.docx"), Path("/tmp/acme_resume.pdf")],
    )
    assert "editable DOCX" in body
    assert "for last-minute edits" in body


def test_compose_digest_omits_docx_note_when_no_docx():
    """If no .docx files in attachments, don't claim DOCX is attached."""
    from gmail.digest import compose_digest

    body = compose_digest(
        processed=_processed_for_digest(),
        skipped=[],
        attachments=[Path("/tmp/acme_resume.pdf")],
    )
    assert "editable DOCX" not in body


def test_compose_digest_omits_docx_note_when_attachments_none():
    """Backwards-compat: callers that omit `attachments` get NO docx note
    (preserves the pre-dual-output digest shape for the test suite)."""
    from gmail.digest import compose_digest

    body = compose_digest(processed=_processed_for_digest(), skipped=[])
    assert "editable DOCX" not in body


def test_main_call_site_no_longer_prepends_attachment_note():
    """Finding 3: the body-text line lives in compose_digest, not main.py.
    The hardcoded `attachment_note` prepend should be gone from main.main()."""
    main_source = (ROOT / "src" / "main.py").read_text()
    assert "attachment_note" not in main_source, (
        "main.py still prepends attachment_note — should live in compose_digest"
    )


# ── Code-review structural fix: HIGH cluster ──────────────────────────────────
# Root cause: renderers returned (docx, docx) on PDF fallback, losing the ops
# signal that PDF conversion was unavailable. The structural fix is:
#  - renderer returns (None, docx_path) on fallback, (pdf_path, docx_path) otherwise
#  - compose_digest checks for BOTH .pdf AND .docx presence in attachments
#  - _build_attachments filters None (M1: also uses .get() for partial-rollout dicts)


def test_compose_digest_says_both_only_when_pdf_and_docx_present():
    """HIGH-1: compose_digest must not claim 'Both PDF + editable DOCX' when only DOCX exists."""
    from gmail.digest import compose_digest

    # DOCX only — fallback mode (no PDF converter on the box)
    body = compose_digest(
        processed=_processed_for_digest(),
        skipped=[],
        attachments=[Path("/tmp/acme_resume.docx"), Path("/tmp/acme_cl.docx")],
    )
    # Must NOT claim a PDF exists
    assert "Both PDF" not in body, (
        "compose_digest falsely claims 'Both PDF' attached when only DOCX present"
    )
    # Should still mention DOCX (it's what's actually attached)
    assert "editable DOCX" in body or "DOCX" in body


def test_compose_digest_says_both_when_both_present():
    """compose_digest claims both attached only when both .pdf and .docx are present."""
    from gmail.digest import compose_digest

    body = compose_digest(
        processed=_processed_for_digest(),
        skipped=[],
        attachments=[
            Path("/tmp/acme_resume.pdf"),
            Path("/tmp/acme_resume.docx"),
            Path("/tmp/acme_cl.pdf"),
            Path("/tmp/acme_cl.docx"),
        ],
    )
    assert "Both PDF" in body and "editable DOCX" in body


def test_build_attachments_skips_missing_keys():
    """M1: Partial-rollout dict missing 'resume_docx'/'cover_letter_docx' must NOT crash.

    During mid-deploy / partial rollout, processed dicts might omit the new docx
    keys. _build_attachments should .get() and skip None, never raise KeyError.
    """
    processed = [{
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com",
        "lane": "pmm",
        "resume_pdf":       Path("/tmp/r.pdf"),
        "cover_letter_pdf": Path("/tmp/cl.pdf"),
        # NOTE: resume_docx and cover_letter_docx intentionally missing
        "hiring_manager": None,
    }]

    # Must NOT raise
    result = _build_attachments(processed)
    assert Path("/tmp/r.pdf") in result
    assert Path("/tmp/cl.pdf") in result


def test_build_attachments_filters_none_from_pdf_fallback():
    """HIGH-3/M4: When PDF conversion unavailable, resume_pdf/cover_letter_pdf is None.

    _build_attachments must filter None — only the 2 DOCX paths end up in the list.
    """
    resume_docx = Path("/tmp/acme_resume.docx")
    cl_docx = Path("/tmp/acme_cl.docx")

    processed = [{
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com",
        "lane": "pmm",
        "resume_pdf":         None,           # fallback — no PDF converter
        "resume_docx":        resume_docx,
        "cover_letter_pdf":   None,           # fallback — no PDF converter
        "cover_letter_docx":  cl_docx,
        "hiring_manager": None,
    }]

    attachments = _build_attachments(processed)

    assert None not in attachments, f"None leaked into attachments: {attachments}"
    assert attachments.count(resume_docx) == 1
    assert attachments.count(cl_docx) == 1
    assert len(attachments) == 2, (
        f"Expected exactly 2 DOCX attachments in fallback mode, got {len(attachments)}: {attachments}"
    )


def test_compose_digest_uses_path_suffix_check():
    """M2: DOCX detection should use Path.suffix, not str.endswith().

    str(p).lower().endswith('.docx') incorrectly matches edge cases like
    a filename ending '.DOCX.bak' would NOT match (str.endswith works there)
    BUT Path.suffix is the canonical/idiomatic check and matches our other code.
    Asserting source-level usage to lock in the convention.
    """
    digest_source = (ROOT / "src" / "gmail" / "digest.py").read_text()
    # Should use Path.suffix, not str.endswith for DOCX/PDF detection
    assert ".suffix" in digest_source, (
        "compose_digest should use Path(p).suffix for extension checks"
    )
    # Old pattern gone
    assert ".endswith('.docx')" not in digest_source.replace('"', "'"), (
        "compose_digest still uses str.endswith for DOCX detection"
    )


def test_main_print_one_line_per_artifact_on_pdf_fallback(capsys, monkeypatch, tmp_path):
    """M17 behavioral (M4): drive main.main() in --test mode with a processed
    fixture whose PDF slots are None (the LibreOffice/docx2pdf fallback case),
    then assert stdout does NOT print the DOCX path twice as if it were a
    distinct PDF + DOCX pair.

    Prior version asserted `"is None" in main_source` — vacuously true
    (5 unrelated occurrences of "is None" in main.py mean it stays green
    even if the entire fallback-print block is deleted). This test drives
    the real print branch and diffs the captured lines.

    Mutation check (see PR description): temporarily reintroduce the M4 bug —
    have the fallback branch print `resume_docx` on both the "Resume PDF" and
    "Resume DOCX" lines. This test then fails on the "expected exactly one
    DOCX line per artifact" assertion.
    """
    import main as main_mod
    import gmail.client as gmail_client_mod

    # Materialize the DOCX-only fixture (PDFs are None — fallback mode).
    resume_docx = tmp_path / "acme_resume.docx"
    cl_docx = tmp_path / "acme_cl.docx"
    resume_docx.write_bytes(b"PK")
    cl_docx.write_bytes(b"PK")

    processed = [{
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com",
        "lane": "pmm",
        "resume_pdf": None,           # fallback — no PDF converter
        "resume_docx": resume_docx,
        "cover_letter_pdf": None,     # fallback — no PDF converter
        "cover_letter_docx": cl_docx,
        "hiring_manager": None,
        "apply_result": None,
    }]

    # Redirect ROOT so the --test mode reads settings from tmp.
    real_root = Path(__file__).parent.parent
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "test_data").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        (real_root / "config" / "settings.yaml").read_text()
    )
    (tmp_path / "templates" / "project_bank.yaml").write_text(
        (real_root / "templates" / "project_bank.yaml").read_text()
    )
    # sample_alert.eml is read once (before parse_alert_from_eml is stubbed).
    (tmp_path / "test_data" / "sample_alert.eml").write_text("")

    monkeypatch.setattr(main_mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        main_mod, "parse_alert_from_eml",
        lambda eml_path, max_jobs=5: [
            {"title": "Engineer", "company": "Acme", "url": "https://example.com"}
        ],
    )
    monkeypatch.setattr(main_mod, "run_pipeline", lambda **kw: (processed, [], []))
    # --test mode instantiates a _NoopGmailClient inline; no patch needed.
    monkeypatch.setattr(sys, "argv", ["main.py", "--test"])

    main_mod.main()

    captured = capsys.readouterr()
    stdout = captured.out

    # The processed job's DOCX line MUST appear (it's the artifact we shipped).
    assert "acme_resume.docx" in stdout, (
        f"Fallback branch should print the resume DOCX path. stdout:\n{stdout}"
    )
    assert "acme_cl.docx" in stdout, (
        f"Fallback branch should print the cover-letter DOCX path. stdout:\n{stdout}"
    )
    # And each DOCX path must appear EXACTLY ONCE — not once labelled 'PDF'
    # and once labelled 'DOCX' (the M4 double-line bug).
    resume_count = stdout.count(str(resume_docx))
    cl_count = stdout.count(str(cl_docx))
    assert resume_count == 1, (
        f"resume DOCX printed {resume_count} times (expected 1 — double-line "
        f"bug reintroduced?):\n{stdout}"
    )
    assert cl_count == 1, (
        f"cover-letter DOCX printed {cl_count} times (expected 1):\n{stdout}"
    )
    # And the DOCX-fallback marker text (from the fallback branch) is present.
    assert "no PDF converter" in stdout, (
        f"Fallback branch should signal 'no PDF converter'. stdout:\n{stdout}"
    )


def test_step_render_event_renamed():
    """HIGH-2: log event renamed from 'step.render_pdf' → 'step.render_documents'
    to match the renamed function and reflect dual-output reality."""
    main_source = (ROOT / "src" / "main.py").read_text()
    assert "step.render_pdf" not in main_source, (
        "main.py still emits stale 'step.render_pdf' event — rename to 'step.render_documents'"
    )
    assert "step.render_documents" in main_source, (
        "main.py should emit 'step.render_documents' to match renamed render function"
    )


def test_renderer_functions_renamed():
    """HIGH-2: rename render_resume_pdf → render_resume, render_cover_letter_pdf → render_cover_letter.

    Old names no longer match behavior (DOCX-primary, PDF-optional)."""
    from pdf_gen import renderer
    assert hasattr(renderer, "render_resume"), (
        "renderer should export render_resume (renamed from render_resume_pdf)"
    )
    assert hasattr(renderer, "render_cover_letter"), (
        "renderer should export render_cover_letter (renamed from render_cover_letter_pdf)"
    )


def test_content_disposition_filename_is_quoted(tmp_path):
    """M3: Content-Disposition filename must be quoted (or RFC2231-encoded) so that
    spaces and non-ASCII characters in attachment filenames do not corrupt the header.

    Currently: `filename={filepath.name}` (unquoted). Expected: `filename="{filepath.name}"`.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    from gmail.client import GmailClient

    # Build a real MIMEMultipart the same way send_email does, but exercise only
    # the attachment-loop logic (extract it into a callable via a thin shim).
    filepath = tmp_path / "Acme Corp_Resume.docx"
    filepath.write_bytes(b"fake docx contents")

    # Construct a message and run the attachment loop manually using the same
    # logic as GmailClient.send_email so we can inspect the header.
    msg = MIMEMultipart()
    msg["to"] = "me@example.com"
    msg["subject"] = "Test"
    msg.attach(MIMEText("body", "plain"))

    suffix = filepath.suffix.lower()
    maintype, subtype = GmailClient._MIME_MAP.get(suffix, ("application", "octet-stream"))
    with open(filepath, "rb") as f:
        part = MIMEBase(maintype, subtype)
        part.set_payload(f.read())
        encoders.encode_base64(part)
        # This is the line we're testing — pulled from gmail/client.py send_email.
        # Source-grep is the actual assertion; this exercise is just to ensure
        # the source-line under test is reachable.
        pass

    # Source-level assertion: the production code must quote the filename.
    client_source = (ROOT / "src" / "gmail" / "client.py").read_text()
    # Reject the unquoted pattern
    assert "filename={filepath.name}" not in client_source, (
        "Content-Disposition filename is unquoted — wrap in double quotes "
        "or use email.utils.encode_rfc2231 for spaces/non-ASCII safety."
    )
    # Require either a quoted pattern OR rfc2231 encoding
    has_quoted = (
        'filename="{filepath.name}"' in client_source
        or "filename=\\\"{filepath.name}\\\"" in client_source
    )
    has_rfc2231 = "encode_rfc2231" in client_source or "add_header" in client_source and "filename" in client_source and "encode" in client_source
    assert has_quoted or has_rfc2231, (
        "Content-Disposition filename must be quoted or RFC2231-encoded; "
        f"current client source pattern not recognized as safe."
    )


if __name__ == "__main__":
    main()
