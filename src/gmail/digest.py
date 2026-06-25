"""
gmail/digest.py — Compose and send the summary digest email.
"""


def compose_digest(
    processed: list[dict],
    skipped: list[dict],
    attachments: list | None = None,
) -> str:
    """Build the plain-text digest email body.

    If `attachments` contains any .docx file, prepend a note explaining the
    PDF + editable-DOCX pairing. This way future callers benefit and the
    note is conditioned on actual DOCX presence (no false claim of an
    editable DOCX when the renderer fell back to PDF-only).
    """
    lines = []

    if attachments and any(str(p).lower().endswith(".docx") for p in attachments):
        lines.append(
            "Both PDF (for direct submission) and editable DOCX "
            "(for last-minute edits in Word/Google Docs) are attached."
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
