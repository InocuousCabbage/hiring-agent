# test_data/EMAIL_FORMAT_ANALYSIS.md
# Reference: Hiring.cafe email structure (Feb 2026)
# Give this to Claude Code when building the parser and JD fetcher.

## Email Metadata
- **From:** `Ali from HiringCafe <ali@hiring.cafe>`
- **Subject pattern:** `Latest Job Postings for {date} | HiringCafe`
- **Format:** multipart/alternative (text/plain + text/html)
- **Can be parsed from .eml files** using Python's `email` module

## HTML Structure

The email contains job "cards" in a 3-column table layout.
Each card is a `<table>` with a single `<td>` containing:

```
<h3><span style="color:#18181a;font-weight:600;">
  {Job Title}
</span></h3>

<div style="color:#18181a;margin-bottom:2px;font-weight:500;">
  {Company} — {Location}
</div>

<div style="color:#75767b;font-size:.92em;margin-bottom:8px;">
  {Date Posted}
</div>

<span style="display:inline-block;background:#d1fae5;color:#047857;...">
  {Salary}  <!-- optional, not all cards have this -->
</span>

<div style="color:#58595d;font-size:.94em;line-height:1.4;">
  {Brief description / requirements snippet}
</div>

<a href="{sendgrid_tracking_url}" style="background:#e26d8c;...">
  Apply
</a>
```

## Parsing Strategy (proven working)

```python
for h3 in soup.find_all("h3"):
    title = h3.find("span").get_text(strip=True)
    card = h3.find_parent("td")

    # Company + Location: first <div> after h3, split on "—"
    company_div = h3.find_next_sibling("div")
    # e.g. "Group O — United States (Remote)"

    # Date: div with color:#75767b
    # Salary: span with background:#d1fae5
    # Description: div with color:#58595d
    # Apply URL: <a> with text "Apply"
```

## URL Chain

All "Apply" links are **SendGrid tracking URLs**:
```
https://u52508838.ct.sendgrid.net/ls/click?upn=...
```

These redirect (HTTP 302) to **hiring.cafe job pages**:
```
https://hiring.cafe/viewjob/{job_id}
```

## CRITICAL: hiring.cafe Has Bot Protection

When fetching `hiring.cafe/viewjob/{id}` with httpx/requests:
- **Returns HTTP 429** with a Vercel Security Checkpoint page
- The page says "We're verifying your browser"
- **You MUST use Playwright** (headless browser) to load these pages

The JD fetcher should:
1. Resolve SendGrid URL → get `hiring.cafe/viewjob/{id}`
2. Use Playwright to load the hiring.cafe page
3. Wait for JS to render the job description
4. Extract the JD content from the rendered page

## Sample Data (6 jobs from Feb 27, 2026 alert)

| # | Title | Company | Location | Salary |
|---|-------|---------|----------|--------|
| 1 | Lead Product Marketing Manager | Group O | United States (Remote) | $55–62.5/hr |
| 2 | Sr. Specialist, Product and Solutions Marketing | Cardinal Health | United States (Remote) | $68,500–88,020/yr |
| 3 | Senior Product Marketing Manager – Financial Close | OneStream | United States (Remote) | $138,000–172,250/yr |
| 4 | Product Managers #IN1176 | Cummins | Columbus, Indiana (Remote) | $110,365–150,000/yr |
| 5 | Senior Marketing Manager - Global Digital Experience | Sinch | United States (Remote) | $123,000–154,000/yr |
| 6 | Product Specialist Demand Generation (BIM) | Allplan | United States (Remote) | $95,000–105,000/yr |

## Resolved URLs

```
Job 1 → https://hiring.cafe/viewjob/stk2j1yk1hyz3uj2
Job 2 → https://hiring.cafe/viewjob/n8n1jn7p9ea02lij
Job 3 → https://hiring.cafe/viewjob/4dzbd8ojxibwtf0c
```
