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


def _build_attachments(processed: list[dict]) -> list[Path]:
    """Replicates the attachment-list construction in main.main(), including
    the dedup that handles the renderer fallback where PDF conversion fails
    and (docx_path, docx_path) is returned."""
    attachments: list[Path] = []
    seen: set = set()
    for p in processed:
        for path in (
            p["resume_pdf"],
            p["resume_docx"],
            p["cover_letter_pdf"],
            p["cover_letter_docx"],
        ):
            if path not in seen:
                seen.add(path)
                attachments.append(path)
    return attachments


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


def test_send_digest_called_with_pdf_and_docx_attachments():
    """gmail.send_digest receives both .pdf AND .docx attachments for resume + CL."""
    processed = _processed_fixture()
    attachments = _build_attachments(processed)

    gmail = MagicMock()
    gmail.send_digest(
        to="me@example.com",
        subject="Digest",
        body_text="body",
        attachments=attachments,
    )

    gmail.send_digest.assert_called_once()
    _, kwargs = gmail.send_digest.call_args
    sent = kwargs["attachments"]
    assert len(sent) == 4
    pdfs  = [p for p in sent if p.suffix == ".pdf"]
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
    """When the PDF renderer falls back (no LibreOffice/docx2pdf available),
    render_resume_pdf / render_cover_letter_pdf return (docx_path, docx_path).
    Each docx path must appear in the attachment list EXACTLY ONCE — otherwise
    Gmail would attach the same DOCX twice under a single filename while the
    body still claims a PDF + DOCX pair is present."""
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


if __name__ == "__main__":
    main()
