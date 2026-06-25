# Master Plan: DOCX + PDF Dual Output

> **Note (post-1658bd6):** Historical planning artifact. Function names `render_resume_pdf` and `render_cover_letter_pdf` were renamed to `render_resume` and `render_cover_letter`. Return type was tightened to `(Optional[Path], Path)`. This doc preserved as-is for audit trail.


**Feature:** Ship BOTH editable DOCX AND PDF as email attachments for resumes + cover letters.

## Discovery Decisions (Ben, 2026-06-25)

1. **#1: ship both** — keep PDF, add DOCX (NOT drop PDF)
2. **#2: keep names** — no `pdf_gen` → `doc_gen` rename
3. **#3: body text** — patch in "editable DOCX" mention; keep otherwise
4. **#4: PDF == DOCX content** — same source, same formatting (already the case since PDF is just docx2pdf of the same DOCX)
5. **#5: backward-compat** — unknown; flag deploy/ scripts as area to audit

## Architecture

DOCX is already the intermediate in `pdf_gen/renderer.py::render_resume_pdf()`:
1. Copy template `.docx`
2. Surgically edit paragraphs/runs
3. Convert DOCX → PDF
4. Return PDF path

Change shape: **expose the DOCX intermediate as a return value alongside the PDF.**

Two ways to do it:
- (a) Return `(pdf_path, docx_path)` tuple from existing functions — breaks call sites
- (b) Add new `render_resume_docx()` / `render_cover_letter_docx()` that returns the docx only; existing PDF functions keep their signature

**Choice: (a)** — return a tuple. Callers update from `pdf = render_resume_pdf(...)` to `pdf, docx = render_resume_pdf(...)`. Simpler than two function pairs (no risk of drift between PDF and DOCX paths producing different content).

## Shards

Single-spec implementation. The change is too small + cross-cutting to shard usefully.

| Spec | Files | Owner |
|---|---|---|
| 01-dual-output | `src/pdf_gen/renderer.py` + `src/main.py` + `src/gmail/client.py` + `tests/test_renderer.py` + `tests/test_full_pipeline.py` + `requirements.txt` (optional) + `README.md` + `SETUP.md` | single writer agent |

## File Ownership

Writer owns ALL files for the change. No parallel writers needed.

## Cross-spec Contracts

N/A — single spec.

## Test Strategy

1. Existing tests (`test_renderer.py`) should still pass with the tuple-return change (update call sites in tests)
2. Add assertions: docx path returned, docx is a valid .docx file (`zipfile.is_zipfile()`), docx has expected paragraph count
3. Add to `test_full_pipeline.py`: both `.pdf` AND `.docx` ATTACHED to the email
4. Gmail client test (if exists): mocked send accepts both file types + correct MIME per extension

## Rollout

- Branch: `feature/docx-dual-output`
- Single PR back to main
- No DB migrations, no env-var changes (DOCX always sent alongside PDF — no opt-in flag)
- Ben merges after review

## Risks

1. **Email size** — DOCX + PDF doubles attachment payload. Gmail attachment limit is 25MB; tailored resumes are typically <1MB so well within limits.
2. **Backward-compat** — `deploy/` scripts unaudited; writer should check + flag.
3. **MIME-type accuracy** — wrong MIME could make some email clients render DOCX as gibberish. `python-docx`-produced files are .docx (ZIP-based OOXML); MIME = `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.

## Validation Gates

1. `pytest tests/` passes
2. Manual: send a test email via main.py with stub job + verify Gmail UI shows BOTH .pdf AND .docx attachments
3. Manual: open .docx in Word/Google Docs and confirm it's editable

## Approval Gates

- Writer returns handoff
- Reviewer evaluates
- **Ben approves before merge** (per his standing rule: don't auto-merge to main)
