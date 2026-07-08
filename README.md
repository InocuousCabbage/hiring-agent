# Hiring.cafe Job Alert Agent

Automated job application pipeline: ingests Hiring.cafe email alerts, fetches JDs,
classifies into marketing lanes, tailors resumes + cover letters, generates PDFs,
and sends a digest email — all hands-off.

Phase 3 adds an opt-in, review-mode default auto-apply stage — Greenhouse only in
the MVP, config-gated, and held behind a Gmail YES/NO reply loop before anything
is submitted. See [docs/apply-flow.md](docs/apply-flow.md) for the operator
manual and [SETUP.md](SETUP.md) for the bootstrap runbook. Enable via
config/settings.yaml apply.enabled=true; default is OFF.

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
- **Claude API** for all LLM reasoning (lane classification, tailoring, QA validation)
- **Idempotent** via Gmail labels + stored message IDs
- **Retry-with-fix** QA loop (max 2 retries) before skipping a job

## Auto-Apply (Phase 3 MVP)

Auto-apply is an opt-in stage that submits Greenhouse-hosted applications on
your behalf, gated on a Gmail YES/NO reply from you. Default posture: off,
review-mode, dry-run held closed. Nothing gets submitted until you explicitly
approve every application and the six checks in `docs/apply-flow.md` pass.

**Safety posture**

- Master switch off by default (`apply.enabled: false`).
- Review mode is the shipped default: the pipeline fills the form,
  screenshots, and stages a Gmail email under
  `hiring-agent/apply/pending`. Only a first-line `YES` re-opens the browser
  and submits. `NO` skips. Ambiguous replies get an auto-clarify.
- 24-hour re-ping, 72-hour auto-decline if no reply.
- `apply.dry_run: true` until the success-criteria checks are green — even
  with `enabled: true`, no submit runs.
- Dedup DB blocks re-applies on the same `(company, ats_domain, ats_job_id)`
  and soft-warns on normalized `(company, role)`.
- Rate cap: 10 applies per ATS per UTC day.
- Computer Use fallback (opt-in) is hard-coded to `review_required` and
  cannot auto-submit.

**Supported ATSes**

- MVP (Phase 3): Greenhouse only.
- Phase 3.5: Lever and Ashby.
- Phase 3.6: Workday and iCIMS (after a Turnstile solve-rate spike).

**Out of scope**

LinkedIn Easy Apply, full-auto default mode, post-submit lifecycle
(screener replies, offers, scheduling), a dashboard over
`state/applied_jobs.db`, and cortextOS Telegram push are all out of scope for
Phase 3.

**Quick start**

See [SETUP.md](SETUP.md#auto-apply-setup) for the two-command bootstrap
runbook and [docs/apply-flow.md](docs/apply-flow.md) for the full pipeline
manual.
