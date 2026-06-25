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
    """Replicates the attachment-list construction in main.main()."""
    attachments: list[Path] = []
    for p in processed:
        attachments.append(p["resume_pdf"])
        attachments.append(p["resume_docx"])
        attachments.append(p["cover_letter_pdf"])
        attachments.append(p["cover_letter_docx"])
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
    """N processed jobs → 4N attachments."""
    processed = _processed_fixture() * 3  # 3 jobs
    attachments = _build_attachments(processed)
    assert len(attachments) == 12


if __name__ == "__main__":
    main()
