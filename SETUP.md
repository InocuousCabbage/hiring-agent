# Hiring Agent — Complete Setup Guide

This guide assumes zero prior experience. Follow every step in order.

---

## What This Does

This is an automated job application pipeline. It:
1. Reads job alert emails from hiring.cafe in your Gmail
2. Fetches the full job description from each posting
3. Classifies the job into a career lane (e.g., Product Marketing, Content, Marketing Ops)
4. Tailors your resume for each specific job using AI
5. Writes a custom cover letter for each job
6. Runs quality checks (no fabricated claims, one page max, etc.)
7. Converts everything to PDF
8. Emails you a digest with all the tailored documents attached

You set it up once, then it runs automatically.

---

## Prerequisites

You need:
- A computer (Mac or Linux — Windows works via WSL)
- Python 3.10 or newer (`python3 --version` to check)
- Node.js 18+ (only if using Claude CLI subscription method)
- A Gmail account
- A hiring.cafe account (free)
- Either a Claude subscription OR an Anthropic API key (for AI tailoring)

---

## Step 1: Clone the Repository

```bash
git clone https://github.com/InocuousCabbage/hiring-agent.git
cd hiring-agent
```

---

## Step 2: Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You must run `source .venv/bin/activate` every time you open a new terminal to work on this project.

---

## Step 3: Install External Tools

### Playwright (headless browser for fetching job descriptions)
```bash
playwright install chromium
```

### LibreOffice (converts DOCX resumes to PDF)
- **Mac:** `brew install --cask libreoffice`
- **Ubuntu/Debian:** `sudo apt install libreoffice`
- **Verify:** `libreoffice --version` should return something

---

## Step 4: Set Up Gmail API Access

This lets the pipeline read your emails and send you the digest.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Click "Select a project" at the top, then "New Project"
3. Name it anything (e.g., "Hiring Agent"), click Create
4. In the search bar, search "Gmail API" and click on it
5. Click "Enable"
6. In the left sidebar, go to "APIs & Services" > "Credentials"
7. Click "Create Credentials" > "OAuth client ID"
8. If prompted to configure consent screen: choose "External", fill in app name (anything), your email, and save
9. Back in Credentials: Application type = "Desktop app", name it anything
10. Click Create — a dialog shows your Client ID and Secret
11. Click "Download JSON" — save the file as `credentials.json`
12. Move it to the project: `mv ~/Downloads/credentials.json config/credentials/`
13. Run the first-time auth:
    ```bash
    python src/gmail/client.py
    ```
    This opens a browser window. Log in with the Gmail account you want alerts sent to. Click "Allow". The terminal will say "Authentication successful" and create a `token.json` file.

---

## Step 5: Set Up Claude (AI for Resume Tailoring)

Pick ONE of these two methods:

### Option A: Anthropic API Key (pay-per-use)
1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Sign up or log in
3. Go to "API Keys" and create a new key
4. Copy the key (starts with `sk-ant-...`)
5. You'll add this to your `.env` file in Step 7

### Option B: Claude CLI with Subscription (uses your Claude Pro/Team plan)
1. Install Node.js if you don't have it: [nodejs.org](https://nodejs.org/)
2. Install Claude Code:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```
3. Log in:
   ```bash
   claude login
   ```
   This opens a browser. Log in with your Claude account and authorize.

---

## Step 6: Set Up Hiring.cafe Alerts

1. Go to [hiring.cafe](https://hiring.cafe) and create an account
2. Set up job alerts for your target roles:
   - Choose your desired job titles (e.g., "Product Marketing Manager", "Marketing Operations")
   - Choose your target locations
   - Set alert frequency (daily recommended)
3. Make sure alerts are sent to the same Gmail account you set up in Step 4
4. Wait for your first alert email to arrive (or trigger one manually on the site)

---

## Step 7: Configure Environment

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in:

```bash
# If using Option A (API key), uncomment and set:
# ANTHROPIC_API_KEY=sk-ant-your-key-here

# If using Option B (CLI), leave ANTHROPIC_API_KEY commented out

# Your Gmail address (same one from Step 4)
MY_EMAIL=your-email@gmail.com

# These match hiring.cafe's email format — usually don't need to change
ALERT_SENDER=ali@hiring.cafe
ALERT_SUBJECT_CONTAINS=HiringCafe
```

---

## Step 8: Create Your Base Resume

The pipeline tailors your resume for each job, but it needs a starting point.

### 8a: Create the DOCX template

1. Open Microsoft Word, Google Docs, or LibreOffice Writer
2. Write your real resume — this is your "base" resume with all your experience
3. Include:
   - Your name and contact info at the top
   - A brief professional summary (2-3 sentences)
   - Work experience (most recent first) — each role should have:
     - Job title, company name, date range
     - 3-6 bullet points describing what you did and the impact
   - Skills section (8-12 relevant skills)
   - Education
4. Keep it to 1 page — the AI will tailor it, not expand it
5. Save as DOCX format (not PDF)
6. Put it in the templates folder:
   ```bash
   cp ~/path/to/your/resume.docx templates/resumes/base_resume.docx
   ```

**Tips for a good base resume:**
- Use real numbers and metrics wherever possible ("Increased X by Y%")
- Include a variety of skills so the AI has material to select from
- Don't worry about tailoring it to a specific job — the AI does that

### 8b: Set up lane-specific resumes (optional)

If you're applying to different types of roles, you can create multiple base resumes:
1. Copy your base resume and adjust emphasis for each lane
2. Save as `templates/resumes/base_pmm.docx`, `base_content.docx`, `base_mops.docx`, etc.
3. Update `config/settings.yaml` — under each lane, set the `template` path:
   ```yaml
   lanes:
     - name: "pmm"
       template: "templates/resumes/base_pmm.docx"
     - name: "content"
       template: "templates/resumes/base_content.docx"
   ```

If you only have one resume, that's fine — just point all lanes to the same file.

---

## Step 9: Create Your Project Bank

The project bank is a YAML file listing your real projects and accomplishments. The AI pulls from this when tailoring your resume and writing cover letters.

1. Open `templates/project_bank.yaml` in a text editor
2. Replace the example projects with YOUR real projects
3. Follow this format for each project:

```yaml
projects:
  - id: "proj_001"
    name: "Name of the project or initiative"
    lane: ["pmm"]  # which job lanes this is relevant to
    company: "Company Name"
    date_range: "2023-2024"
    summary: >
      One paragraph describing what the project was and your role in it.
    bullets:
      - "Specific accomplishment with a number or metric"
      - "Another accomplishment — be concrete"
      - "What you built, led, improved, or delivered"
    metrics:
      - "Revenue/pipeline/growth numbers if available"
      - "Efficiency gains, cost savings, scale achieved"
    tools_used:
      - "Tool 1"
      - "Tool 2"
    tags:
      - "relevant keyword"
      - "another keyword"
```

**Tips:**
- Include 5-15 projects (more = more material for the AI to work with)
- Use real metrics — "Increased conversion rate by 34%" beats "Improved conversions"
- Tag each project with the lane(s) it's relevant to: `pmm`, `content`, `mops`, or multiple
- The `tools_used` field helps the AI match your experience to job requirements

---

## Step 10: Configure Job Lanes (Optional)

Open `config/settings.yaml` to customize:

- **lanes**: The job categories and their keyword signals. The default lanes are Product Marketing, Content Marketing, and Marketing Ops. Change these to match your career targets.
- **resume.max_roles_to_edit**: How many work experience entries the AI can modify (default: 3)
- **resume.min_confidence_score**: Skip jobs where the AI rates the fit below this score (default: 30 out of 100)
- **cover_letter.tone**: "neutral", "warm", or "assertive"
- **jobs.max_per_run**: How many jobs to process per run (default: 5)

---

## Step 11: Test Run

```bash
source .venv/bin/activate
python src/main.py
```

The first run will:
1. Check your Gmail for hiring.cafe alert emails
2. Fetch job descriptions from each posting
3. Classify, tailor, generate cover letters, QA check, and create PDFs
4. Email you a digest with all documents attached

Check the `output/` folder for the generated files. Check `logs/` if something goes wrong.

If you don't have any alert emails yet, wait for one from hiring.cafe, then run again.

---

## Step 12: Automate (Optional)

Run the pipeline automatically every 2 days:

### Mac/Linux cron:
```bash
crontab -e
```
Add this line:
```
0 9 */2 * * cd ~/hiring-agent && source .venv/bin/activate && python src/main.py >> logs/pipeline.log 2>&1
```
This runs at 9 AM every other day.

### Verify it's set:
```bash
crontab -l
```

---

## Troubleshooting

**"Invalid API key"**
- If using Option A: check that ANTHROPIC_API_KEY is set correctly in `.env`
- If using Option B: run `claude login` again

**"No alert emails found"**
- Check that hiring.cafe alerts are going to the right Gmail account
- Check `config/settings.yaml` — the `alert_sender` and `alert_subject_contains` must match your actual alert emails

**"Gmail auth failed"**
- Delete `config/credentials/token.json` and run `python src/gmail/client.py` again

**"LibreOffice not found"**
- Make sure LibreOffice is installed and `libreoffice --version` works
- On Mac, you may need to add it to PATH: `export PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"`

**"Playwright browser not found"**
- Run `playwright install chromium` again

---

## File Structure Reference

```
hiring-agent/
├── config/
│   ├── settings.yaml           # All settings (lanes, limits, tone, etc.)
│   └── credentials/
│       ├── credentials.json    # Gmail OAuth creds (you create this)
│       └── token.json          # Auto-created after first Gmail auth
├── templates/
│   ├── resumes/
│   │   └── base_resume.docx    # YOUR base resume (you create this)
│   ├── cover_letter.docx       # Cover letter template
│   └── project_bank.yaml       # YOUR projects and accomplishments
├── output/                     # Generated PDFs and DOCX files
├── logs/                       # Pipeline logs
├── src/                        # Source code (don't need to modify)
├── .env                        # Your environment config (you create this)
└── requirements.txt            # Python dependencies
```
