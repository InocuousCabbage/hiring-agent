"""
parser/email_parser.py — Extract job entries from a Hiring.cafe alert email.

Actual Hiring.cafe email format (as of Feb 2026):
  - Sender: ali@hiring.cafe
  - Subject: "Latest Job Postings for {date} | HiringCafe"
  - Jobs are in <table> cards, 3 per row
  - Each card contains:
      <h3><span> Job Title </span></h3>
      <div> Company — Location </div>
      <div> Date posted </div>
      <span> Salary (optional) </span>
      <div> Brief description </div>
      <a href="sendgrid-tracking-url">Apply</a>

  - Apply URLs are SendGrid tracking links that redirect to:
      https://hiring.cafe/viewjob/{job_id}
"""

import email as email_lib
from email import policy
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import httpx
import structlog

log = structlog.get_logger()


def parse_alert_from_eml(eml_path: str | Path, max_jobs: int = 5) -> list[dict]:
    """
    Parse job entries directly from a .eml file.
    Returns list of job dicts, limited to max_jobs.
    """
    with open(eml_path, encoding="utf-8", errors="replace") as f:
        msg = email_lib.message_from_file(f, policy=policy.default)

    html_body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html_body = part.get_content()
            break

    if not html_body:
        log.warning("parser.no_html_in_eml")
        return []

    return parse_alert_email(html_body=html_body, max_jobs=max_jobs)


def parse_alert_email(
    html_body: str,
    text_body: str = "",
    max_jobs: int = 5,
) -> list[dict]:
    """
    Parse job entries from the alert email HTML.

    Returns list of dicts:
      [{
        "title": str,
        "company": str,
        "location": str | None,
        "date_posted": str | None,
        "salary": str | None,
        "description_snippet": str | None,
        "url": str,               # SendGrid tracking URL
        "resolved_url": str | None # Actual hiring.cafe URL (resolved lazily)
      }, ...]
    """
    if not html_body:
        log.warning("parser.no_html_body")
        return []

    soup = BeautifulSoup(html_body, "lxml")
    jobs = []
    # M7 fix: key dedup on (title, company) — same-titled roles at different
    # companies are legitimately different jobs and must both enter the
    # pipeline. Keying on title alone silently dropped the second card,
    # with no log line, on every alert containing two "Product Marketing
    # Manager"s (or similar) at different employers.
    #
    # Phase 5 iter-2 (finding #4): when company parsing falls back to the
    # 'Unknown' sentinel for two distinct cards with the same title, keying
    # on (title, 'Unknown') collides and silently drops one — the exact
    # audit failure M7 was written to prevent. When company is 'Unknown',
    # fall back to (title, url) so distinct apply URLs identify distinct
    # jobs.
    seen_title_company: set[tuple[str, str]] = set()

    # Each job is in an <h3> tag inside a table card
    for h3 in soup.find_all("h3"):
        title_span = h3.find("span")
        title = (title_span.get_text(strip=True) if title_span
                 else h3.get_text(strip=True))

        if not title or len(title) < 3:
            continue

        # Navigate to the parent <td> which contains the full card
        card = h3.find_parent("td")
        if not card:
            continue

        # Extract company + location from first <div> after h3
        company = "Unknown"
        location = None
        company_div = h3.find_next_sibling("div")
        if company_div:
            raw = company_div.get_text(strip=True)
            if "—" in raw:
                parts = raw.split("—", 1)
                company = parts[0].strip()
                location = parts[1].strip()
            elif "–" in raw:
                parts = raw.split("–", 1)
                company = parts[0].strip()
                location = parts[1].strip()
            elif raw:
                company = raw

        # Extract date posted (second div — has lighter color styling)
        date_posted = None
        date_divs = card.find_all("div", style=lambda s: s and "75767b" in s)
        if date_divs:
            date_posted = date_divs[0].get_text(strip=True)

        # Extract salary (in a <span> with green background)
        salary = None
        salary_span = card.find("span", style=lambda s: s and "d1fae5" in s)
        if salary_span:
            salary = salary_span.get_text(strip=True)

        # Extract description snippet (div with color:#58595d)
        description_snippet = None
        desc_divs = card.find_all("div", style=lambda s: s and "58595d" in s)
        if desc_divs:
            description_snippet = desc_divs[0].get_text(strip=True)

        # Extract Apply URL — MUST happen BEFORE the dedup slot is claimed
        # (Phase 5 iter-2 A3 fix). A card with a missing/broken URL would
        # otherwise reserve the (title, company) slot and silently drop the
        # next valid card with the same pair — reintroducing the exact
        # class of bug M7 was written to prevent.
        apply_link = card.find("a", string=lambda s: s and "Apply" in s.strip())
        if not apply_link or not apply_link.get("href"):
            continue

        url = apply_link["href"].strip()

        # M7 + Phase 5 iter-2: build the dedup key AFTER url is validated.
        # For 'Unknown' company (parser fell back), key on URL instead so
        # two distinct Unknown-company jobs both surface.
        dedup_key = (title, url) if company == "Unknown" else (title, company)
        if dedup_key in seen_title_company:
            continue
        seen_title_company.add(dedup_key)

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "date_posted": date_posted,
            "salary": salary,
            "description_snippet": description_snippet,
            "url": url,
            "resolved_url": None,  # Resolved later by jd_fetcher
        })

    log.info("parser.extracted", job_count=len(jobs))
    return jobs[:max_jobs]


def resolve_sendgrid_url(tracking_url: str, timeout: int = 10) -> str | None:
    """
    Follow SendGrid tracking URL redirects to get the actual hiring.cafe URL.

    SendGrid URLs redirect like:
      sendgrid.net/ls/click?... → hiring.cafe/viewjob/{id}

    Returns the final URL or None on failure.
    """
    try:
        resp = httpx.get(
            tracking_url,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            },
        )
        final_url = str(resp.url)
        log.debug("parser.resolved_url", original=tracking_url[:60], final=final_url)
        return final_url
    except Exception as e:
        log.warning("parser.resolve_failed", error=str(e))
        return None
