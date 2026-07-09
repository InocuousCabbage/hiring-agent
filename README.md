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
│   │   └── base_resume.docx   # Your base resume (all lanes point here by default)
│   ├── candidate_profile.yaml.example  # Auto-apply profile schema (Phase 3)
│   └── project_bank.yaml      # Your real projects + metrics
├── src/
│   ├── main.py                # Orchestrator — runs the full pipeline
│   ├── llm.py                 # Claude API + Claude-CLI subprocess wrapper
│   ├── gmail/                 # Gmail API auth + read/send/label + digest
│   ├── parser/                # Extract job entries from alert HTML
│   ├── scraper/               # Fetch + clean job descriptions
│   ├── classifier/            # PMM vs Content vs MOps classification
│   ├── tailor/                # Resume tailoring + cover letter generation
│   ├── pdf_gen/               # DOCX build + LibreOffice PDF export
│   ├── qa/                    # QA checklist + auto-fix loop
│   ├── contacts/              # Hiring-manager finder (opt-in)
│   ├── browser/               # Shared Playwright session (Phase 3)
│   └── apply/                 # Auto-apply pipeline (Phase 3, opt-in)
│       ├── adapters/          # Per-ATS adapters (greenhouse, computer_use)
│       ├── migrations/        # SQLite schema (applied_jobs, review_pending)
│       ├── transport/         # Local + Browserbase Playwright transport
│       ├── bootstrap.py       # `python -m src.apply.bootstrap <ats>`
│       ├── dedup.py           # applied_jobs + `--unblock` CLI
│       ├── dispatcher.py      # URL → ATS routing
│       ├── review.py          # YES/NO review loop + poller
│       └── ...
├── tests/                     # Offline unit + integration + apply/ live gates
├── docs/
│   └── apply-flow.md          # Auto-apply operator manual
├── requirements.txt
├── .env.example
├── SETUP.md
└── deploy/
    ├── Dockerfile
    └── cron_entry.sh          # Cron entrypoint (flock-guarded)
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
4. Run `python -m src.gmail.client` once to complete the OAuth flow

### 4. Configure
```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY (Option A) and MY_EMAIL
```

### 5. Prepare your content
- Place your base resume at `templates/resumes/base_resume.docx` (all lanes
  point here by default; see SETUP §Step 8 for optional per-lane variants
  like `base_pmm.docx`, `base_content.docx`, `base_mops.docx`).
- Fill out `templates/project_bank.yaml` with your real projects + metrics.
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
