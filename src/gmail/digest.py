"""
gmail/digest.py — Compose and send the summary digest email.
"""

from pathlib import Path


def compose_digest(
    processed: list[dict],
    skipped: list[dict],
    attachments: list | None = None,
) -> str:
    """Build the plain-text digest email body.

    Adds an attachment-format note conditioned on what is ACTUALLY in
    `attachments` — checked via Path(p).suffix (canonical), not str.endswith():

      - PDFs and DOCX both present  → "Both PDF + editable DOCX attached"
      - DOCX present, no PDFs       → "Editable DOCX attached (no PDF
                                       converter installed on the box)"
      - No DOCX                     → no note (preserves pre-dual-output
                                       digest shape for callers that omit
                                       attachments entirely)
    """
    lines = []

    if attachments:
        suffixes = {Path(p).suffix.lower() for p in attachments}
        has_pdf = ".pdf" in suffixes
        has_docx = ".docx" in suffixes
        if has_pdf and has_docx:
            lines.append(
                "Both PDF (for direct submission) and editable DOCX "
                "(for last-minute edits in Word/Google Docs) are attached."
            )
            lines.append("")
        elif has_docx and not has_pdf:
            lines.append(
                "Editable DOCX attached (for last-minute edits in "
                "Word/Google Docs) — no PDF converter is installed, so the "
                "PDF is missing. Install LibreOffice on the agent box to "
                "restore the PDF + DOCX pair."
            )
            lines.append("")

    lines.append(f"Processed ({len(processed)})")
    lines.append("=" * 40)
    for job in processed:
        location = job.get("location", "Unknown")
        lines.append(f"  {job['title']} — {job['company']} ({location})")
        lines.append(f"  Lane: {job.get('lane', 'N/A')}")
        lines.append(f"  URL: {job['url']}")
        hm = job.get("hiring_manager")
        if hm:
            lines.append(f"  Hiring Manager: {hm.get('name', 'Unknown')} — {hm.get('title', 'N/A')} ({hm.get('confidence', 'N/A')})")
            if hm.get("linkedin_url"):
                lines.append(f"  LinkedIn: {hm['linkedin_url']}")
            if hm.get("email"):
                lines.append(f"  Email: {hm['email']}")
            if hm.get("outreach_note"):
                lines.append(f"  Outreach: {hm['outreach_note']}")
        lines.append("")

    if skipped:
        lines.append(f"\nSkipped ({len(skipped)})")
        lines.append("=" * 40)
        for job in skipped:
            lines.append(f"  {job['title']} — {job['company']}")
            lines.append(f"  URL: {job['url']}")
            lines.append(f"  Reason: {job.get('reason', 'Unknown')}")
            lines.append("")

    lines.append("\n— Hiring Agent (automated)")
    return "\n".join(lines)
