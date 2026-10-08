"""
qa/copy_gate.py: run the vendored copy scanner over in-memory resume and
cover-letter text and map each hit back to the field it came from.

The rule logic lives only in copy_scan.py (vendored, do not fork it). This
adapter writes one line per field to a temp file, calls copy_scan.scan() on
it, and translates line numbers back to field names.

Two deliberate choices about how scan() is called:

1. skip_meta=False. scan() skips "documentation" lines that contain words
   such as "never", "avoid", "example" or "rule". That heuristic is for docs
   that DISCUSS a tell. Generated application copy is never documentation,
   and a cover letter line like "I never miss a deadline" would otherwise be
   silently exempt from every rule.

2. Blocking follows the ORIGINAL rule severity. Upstream lowers a quoted hit
   one step (error to warn, warn to info) but still treats a quoted error as
   blocking. Here an error-level rule is always a QA error, a warn-level rule
   is a logged warning, and a quoted warn (info) is dropped.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from . import copy_scan

COPY_ERROR_PREFIX = "copy check failed: "


@dataclass(frozen=True)
class CopyHit:
    rule_id: str
    severity: str  # the rule's own severity: "error" or "warn"
    matched: str
    field: str
    quoted: bool

    @property
    def message(self) -> str:
        return f"{COPY_ERROR_PREFIX}{self.rule_id}: '{self.matched}' in {self.field}"


def scan_fields(fields: dict[str, str]) -> tuple[list[CopyHit], list[CopyHit]]:
    """Scan {field_name: text} and return (error_hits, warn_hits)."""
    line_fields: list[str] = []
    lines: list[str] = []
    for name, text in fields.items():
        for part in (text or "").splitlines() or [""]:
            line_fields.append(name)
            lines.append(part)

    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="copy_gate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        hits, _rc = copy_scan.scan(tmp, skip_meta=False)
    finally:
        os.unlink(tmp)

    errors: list[CopyHit] = []
    warnings: list[CopyHit] = []
    for line_no, rule_id, severity, matched, _why, quoted in hits:
        hit = CopyHit(rule_id, severity, matched, line_fields[line_no - 1], bool(quoted))
        if severity == "error":
            errors.append(hit)
        elif severity == "warn" and not quoted:
            warnings.append(hit)
    return errors, warnings


def copy_fields(tailored_resume: dict, cover_letter: dict) -> dict[str, str]:
    """Collect the reader-facing text fields that go into the rendered files."""
    fields: dict[str, str] = {}
    for i, para in enumerate(cover_letter.get("paragraphs", []) or []):
        fields[f"cover_letter.paragraphs[{i}]"] = para if isinstance(para, str) else ""
    summary = tailored_resume.get("summary", "")
    fields["resume.summary"] = summary if isinstance(summary, str) else ""
    for ri, role in enumerate(tailored_resume.get("roles", []) or []):
        for bi, bullet in enumerate(role.get("bullets", []) or []):
            fields[f"resume.roles[{ri}].bullets[{bi}]"] = bullet if isinstance(bullet, str) else ""
    return fields
