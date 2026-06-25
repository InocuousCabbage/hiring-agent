# Spec 01: Dual DOCX + PDF Output

> **Note (post-1658bd6):** Historical planning artifact. Function names `render_resume_pdf` and `render_cover_letter_pdf` were renamed to `render_resume` and `render_cover_letter`. Return type was tightened to `(Optional[Path], Path)`. This doc preserved as-is for audit trail.


## Objective

Modify the hiring-agent's email output so it sends BOTH a PDF AND an editable DOCX of the optimized resume + cover letter. The DOCX is the same content as the PDF — it IS the source the PDF is converted from. Recipients can edit the DOCX in Word / Google Docs / LibreOffice before submitting.

## Owned files (writer can edit)

- `src/pdf_gen/renderer.py`
- `src/main.py`
- `src/gmail/client.py`
- `tests/test_renderer.py`
- `tests/test_full_pipeline.py`
- `tests/test_review_fixes.py`
- `README.md`
- `SETUP.md`

## Files writer may READ but not edit

- `requirements.txt` (only edit if a new dep is needed — none expected)
- `config/` (read for stub values in tests)
- `templates/resumes/README.md` (read for template context)
- `deploy/` (audit for any .pdf-hardcoded paths and flag in handoff)

## Provided contracts

After this spec:

```python
# pdf_gen/renderer.py
def render_resume_pdf(
    tailored_resume: dict,
    lane: dict,
    output_dir: Path,
    job_slug: str,
) -> tuple[Path, Path]:
    """
    Returns (pdf_path, docx_path) — both files are written to output_dir.
    DOCX is the editable intermediate; PDF is the converted final.
    Content is byte-identical in semantic terms (same source DOCX).
    """

def render_cover_letter_pdf(
    cover_letter: dict,
    lane: dict,
    output_dir: Path,
    job_slug: str,
) -> tuple[Path, Path]:
    """Same shape: returns (pdf_path, docx_path)."""
```

```python
# gmail/client.py — MIME type per extension
# Update send_email to dispatch MIMEBase subtype by file suffix:
#   .pdf  → MIMEBase("application", "pdf")
#   .docx → MIMEBase("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
#   default → MIMEBase("application", "octet-stream")
```

## Consumed contracts

None — this spec doesn't depend on other in-flight specs.

## Implementation Steps

1. **`src/pdf_gen/renderer.py`** — surface the DOCX path
   - In `render_resume_pdf()` and `render_cover_letter_pdf()`, the existing implementation already writes a `.docx` file to disk before the PDF conversion. Locate that variable + return it as the second element of a tuple.
   - Keep all existing PDF behavior unchanged.
   - Add type annotation `-> tuple[Path, Path]`.
   - DO NOT rename the functions.

2. **`src/main.py`** — handle tuple return + pass BOTH files to email
   - Update unpacking: `resume_pdf, resume_docx = render_resume_pdf(...)` and similar for cover letter.
   - Build the attachments list as `[resume_pdf, resume_docx, cl_pdf, cl_docx]` (or whatever order matches existing logging).
   - Update the email body text: insert one new line mentioning the editable DOCX. Suggested copy: "Both PDF (for direct submission) and editable DOCX (for last-minute edits in Word/Google Docs) are attached." Keep the rest of the body as-is.
   - Update `job_log.info("step.render_pdf", ...)` to log all 4 paths (existing 2 PDFs + 2 DOCX).

3. **`src/gmail/client.py`** — MIME-type dispatch by extension
   - Replace the hardcoded `MIMEBase("application", "pdf")` with a small dispatch:
     ```python
     suffix = filepath.suffix.lower()
     mime_map = {
         ".pdf": ("application", "pdf"),
         ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
     }
     maintype, subtype = mime_map.get(suffix, ("application", "octet-stream"))
     part = MIMEBase(maintype, subtype)
     ```
   - Update the docstring on `send_email` and `send_digest` from "with optional PDF attachments" → "with optional PDF/DOCX attachments".
   - No other changes to send logic.

4. **`tests/test_renderer.py`** — update call-site unpacking + add DOCX assertions
   - Wherever `render_resume_pdf(...)` is called, update to tuple unpack.
   - Add new assertions per call site:
     ```python
     import zipfile
     assert docx_path.exists()
     assert zipfile.is_zipfile(docx_path)  # DOCX is OOXML/ZIP
     from docx import Document
     doc = Document(docx_path)
     assert len(doc.paragraphs) > 20  # base_resume.docx has 26+ paragraphs
     ```
   - Same shape for `render_cover_letter_pdf`.
   - Existing PDF checks stay.

5. **`tests/test_full_pipeline.py`** — confirm both attachments
   - Update test to assert that the mocked `send_email` was called with attachments list containing BOTH `.pdf` AND `.docx` files for resume AND cover letter (4 total).
   - If the test currently inspects the count or filenames, update assertions.

6. **`tests/test_review_fixes.py`** — only update if it imports or stubs renderer functions
   - Otherwise leave alone.

7. **`README.md`** — update output description
   - Find the section describing the email output and update it to mention DOCX is included for editing.
   - One sentence + a bullet, no rewrite.

8. **`SETUP.md`** — update output description
   - Same shape — one sentence + a bullet mentioning DOCX is attached for editing.
   - LibreOffice install instructions STAY (PDF is still produced).

9. **`deploy/`** — audit only, no edit
   - Grep for any hardcoded `.pdf` paths in deploy scripts. If any found, note in handoff. Don't modify deploy scripts unless they're clearly broken by the tuple-return.

## Validation

After implementation:

```bash
cd /tmp/hiring-agent-history
# Activate venv if needed
python -m pytest tests/ -v
```

ALL tests must pass. If any test fails because it can't actually run (missing LibreOffice, missing real templates), mark `@pytest.mark.skip` with explicit reason in the handoff.

Additional manual check (writer doesn't run this — Ben will):
- Run `python -m src.main` with stub config + verify Gmail UI shows BOTH .pdf AND .docx attachments
- Open .docx in Word; verify it opens + is editable

## Handoff requirements

Return:
- List of files changed with line-count delta
- Test run output (full pytest -v)
- Confirmation: 4 attachments built (2 PDFs + 2 DOCX) per email
- Any skipped tests with reason
- `deploy/` audit result (any hardcoded .pdf paths found?)
- Sample of the new email body text
- Risks / open questions for Ben to review
