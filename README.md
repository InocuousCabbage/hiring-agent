# Hiring.cafe Job Alert Agent

Automated job application pipeline: ingests Hiring.cafe email alerts, fetches JDs,
classifies into marketing lanes, tailors resumes + cover letters, generates PDFs
**and editable DOCX files**, and sends a digest email — all hands-off.

## Architecture

```
Gmail Alert → Parse Jobs → Fetch JDs → Classify Lane → Tailor Resume + CL → QA → PDF → Digest Email
```

## Project Structure

```
hiring-agent/
├── config/
│   ├── settings.yaml          # All configurable knobs
│   └── credentials/           # Gmail OAuth creds (gitignored)
├── templates/
│   ├── resumes/
│   │   ├── base_pmm.docx      # Product Marketing base resume
│   │   ├── base_content.docx  # Content Marketing base resume
│   │   └── base_mops.docx     # Marketing Ops base resume
│   ├── cover_letter.docx      # Cover letter template
│   └── project_bank.yaml      # Your real projects + metrics
├── src/
│   ├── main.py                # Orchestrator — runs the full pipeline
│   ├── gmail/
│   │   ├── __init__.py
│   │   ├── client.py          # Gmail API auth + read/send/label
│   │   └── digest.py          # Compose + send the digest email
│   ├── parser/
│   │   ├── __init__.py
│   │   └── email_parser.py    # Extract job entries from alert HTML
│   ├── scraper/
│   │   ├── __init__.py
│   │   └── jd_fetcher.py      # Fetch + clean job descriptions
│   ├── classifier/
│   │   ├── __init__.py
│   │   └── lane_selector.py   # PMM vs Content vs MOps classification
│   ├── tailor/
│   │   ├── __init__.py
│   │   ├── resume_tailor.py   # Resume tailoring via Claude API
│   │   └── cover_letter.py    # Cover letter generation
│   ├── pdf_gen/
│   │   ├── __init__.py
│   │   └── renderer.py        # DOCX template fill → PDF export
│   └── qa/
│       ├── __init__.py
│       └── checker.py         # QA checklist + auto-fix loop
├── tests/
│   └── ...
├── requirements.txt
├── .env.example
└── deploy/
    ├── Dockerfile
    └── cron_entry.sh
```

## Setup

### 1. Prerequisites
- Python 3.11+
- LibreOffice (for DOCX → PDF conversion)
- Google Cloud project with Gmail API enabled

### 2. Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Gmail OAuth
1. Go to Google Cloud Console → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Desktop app)
3. Download `credentials.json` → place in `config/credentials/`
4. Run `python src/gmail/client.py` once to complete the OAuth flow

### 4. Configure
```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and paths
```

### 5. Prepare your content
- Place your 3 base resume .docx files in `templates/resumes/`
- Fill out `templates/project_bank.yaml` with your real projects + metrics
- Edit `config/settings.yaml` for alert sender, labels, etc.

### 6. Run
```bash
# Manual run
python src/main.py

# Or deploy with cron (see deploy/)
```

## Deployment Options

| Option | Cost | Complexity |
|--------|------|------------|
| Local cron | Free | Low |
| Railway | ~$5/mo | Low |
| Google Cloud Function + Scheduler | ~$1/mo | Medium |
| DigitalOcean droplet | $4-6/mo | Medium |

## Key Design Decisions

- **DOCX templates → PDF** (not HTML→PDF) for maximum style fidelity to your base resumes
- **Dual output** — the digest email attaches BOTH the PDF (for direct submission) and the editable DOCX (for last-minute edits in Word / Google Docs / LibreOffice)
- **Claude API** for all LLM reasoning (lane classification, tailoring, QA validation)
- **Idempotent** via Gmail labels + stored message IDs
- **Retry-with-fix** QA loop (max 2 retries) before skipping a job
