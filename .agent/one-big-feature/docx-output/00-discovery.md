# Feature Discovery: DOCX Output (instead of PDF)

> **Note (post-1658bd6):** Historical planning artifact. Function names `render_resume_pdf` and `render_cover_letter_pdf` were renamed to `render_resume` and `render_cover_letter`. Return type was tightened to `(Optional[Path], Path)`. This doc preserved as-is for audit trail.


**Started:** 2026-06-25
**Repo:** github.com/InocuousCabbage/hiring-agent
**Repo state:** 1 commit (4d7c450 "Initial public release"), 41 files, fresh-public

## Feature Request

Change the hiring-agent's email output so optimized resumes + cover letters are sent as **DOCX (editable Word format)** instead of PDFs, so recipients can edit before submitting.

## Codebase Reconnaissance (complete)

### Current architecture

The pipeline ALREADY uses DOCX as the intermediate format. PDF is the final conversion step:

1. `src/pdf_gen/renderer.py::render_resume_pdf()`:
   - Copy lane's base `.docx` template
   - Surgically replace writable sections (tagline para 3, summary para 6, role bullets paras 12/14-17/20/23, skills table) via python-docx
   - Convert DOCX → PDF via LibreOffice (primary, env `LIBREOFFICE_PATH`) or `docx2pdf` (macOS fallback)
   - Return PDF path

2. `src/pdf_gen/renderer.py::render_cover_letter_pdf()`:
   - Same flow for cover letter

3. `src/main.py`:
   ```python
   from pdf_gen.renderer import render_resume_pdf, render_cover_letter_pdf
   resume_pdf = render_resume_pdf(...)
   cl_pdf = render_cover_letter_pdf(...)
   ```

4. `src/gmail/client.py::send_email()`:
   - Attaches files with hardcoded `MIMEBase("application", "pdf")`
   - Body text mentions PDF (TBD — need to grep)

### Implications

This is a SMALLER change than a typical one-big-feature warrants:
- DOCX is already generated as the intermediate — just stop converting + return docx path
- Email MIME type changes from `application/pdf` to `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- LibreOffice / `docx2pdf` / `pypdf` (used for QA page-count) dependencies become optional or removable
- Tests in `tests/test_renderer.py` need updating (likely check PDF output today)
- Templates (`templates/resumes/*.docx`) are user-provided, gitignored — no change needed

### Cross-module touch

- `src/pdf_gen/renderer.py` (rename + return DOCX path)
- `src/main.py` (rename imports + variables)
- `src/gmail/client.py` (MIME type + body text)
- `requirements.txt` (likely drop pypdf / docs change for LibreOffice)
- `tests/test_renderer.py` (update output-format checks)
- `tests/test_full_pipeline.py` (likely needs DOCX assertions)
- `SETUP.md` (likely mentions PDF setup steps)
- `README.md` (mentions PDF output)
- Module rename: `src/pdf_gen/` → `src/doc_gen/` (optional cleanup, since "pdf_gen" is misnomer post-change)

## Open Discovery Questions (sent to Ben)

1. **Drop PDF entirely OR ship both?** Determines whether we delete LibreOffice/docx2pdf code + drop pypdf, or keep PDF as an opt-in (e.g. `OUTPUT_FORMAT=docx|pdf|both` env)
2. **Module/function rename?** `pdf_gen/renderer.py::render_resume_pdf()` → `doc_gen/renderer.py::render_resume_docx()`, or keep names + just change behavior
3. **Email body text** — full rewrite, or just s/PDF/editable DOCX/?
4. **Test coverage** — existing tests probably check PDF page count etc.; what's the DOCX equivalent (e.g. paragraph index integrity, font preserved, length within 1-page-equivalent word count)?
5. **Backward compatibility** — any existing user dotfiles, run scripts, env vars referencing `.pdf` outputs?
