# Resume Templates

Place your base resume DOCX file(s) here. These are gitignored because they contain personal data.

## Required

- `base_resume.docx` -- Your base resume in DOCX format (all lanes point here by default)

## Optional (lane-specific templates)

If you want different base resumes per lane, create separate files and update `config/settings.yaml`:

- `base_pmm.docx` -- Product Marketing resume
- `base_content.docx` -- Content Marketing resume
- `base_mops.docx` -- Marketing Ops resume

## How to Create Your Base Resume

See `SETUP.md` Step 8 for detailed instructions. Key points:

1. Write your real resume in Word, Google Docs, or LibreOffice Writer
2. Include: name, contact info, summary, work experience (with bullets), skills, education
3. Keep it to 1 page
4. Save as `.docx` format
5. Place it here as `base_resume.docx`

The pipeline reads the DOCX structure to understand paragraph indices, so the format matters.
See the paragraph map in `src/pdf_gen/renderer.py` for how indices are mapped.
