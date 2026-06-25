"""
scraper/jd_fetcher.py — Fetch and clean job descriptions from job postings.

Flow:
  1. Search Google for ATS posting (greenhouse.io, lever.co, etc.) using job title + company
  2. If found, scrape the ATS page directly (fast, reliable)
  3. Fall back to hiring.cafe URL via Playwright (hiring.cafe has Cloudflare bot protection)
  4. Validate: ≥ min_length chars AND at least one JD section header
  5. Clean and return the text, or None on failure
"""

import re
import time
import httpx
import structlog
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from parser.email_parser import resolve_sendgrid_url

log = structlog.get_logger()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Section header patterns that confirm real JD content
JD_SECTION_PATTERNS = [
    r"(?i)(responsibilities|what you.?ll do|the role|about the role)",
    r"(?i)(requirements|qualifications|what we.?re looking for|who you are)",
    r"(?i)(preferred|nice.to.have|bonus|ideal candidate)",
    r"(?i)(benefits|perks|compensation|what we offer)",
    r"(?i)(about us|about the company|about \w+)",
]

# Known ATS domains where external job postings live
ATS_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "icims.com",
    "brassring.com",
    "smartrecruiters.com",
    "jobvite.com",
    "taleo.net",
    "successfactors.com",
    "bamboohr.com",
    "ashbyhq.com",
    "rippling.com",
]


def fetch_job_description(
    url: str,
    timeout: int = 30,
    min_length: int = 200,
    job_title: str = "",
    company: str = "",
) -> str | None:
    """
    Fetch and return cleaned JD text for a job URL, or None on failure.

    Strategy (in order):
      1. Google search for ATS posting using job_title + company
      2. If ATS URL found, scrape it directly
      3. Fall back to hiring.cafe URL via Playwright
      4. Return None if everything fails

    Returns None if:
      - No ATS posting found via search AND hiring.cafe is blocked
      - Extracted text is shorter than min_length
      - No recognizable JD section headers found
    """
    # Step 1: Try Google search for direct ATS posting
    if job_title and company:
        ats_url = _search_for_jd(job_title, company)
        if ats_url:
            log.info("jd_fetcher.google_found_ats", url=ats_url)
            ats_text = _fetch_ats_page(ats_url, timeout)
            if ats_text and len(ats_text) >= min_length and _has_jd_sections(ats_text):
                log.info("jd_fetcher.success", url=ats_url, chars=len(ats_text), source="google_ats")
                return _clean_text(ats_text)
            log.debug("jd_fetcher.google_ats_insufficient", chars=len(ats_text) if ats_text else 0)

        # Step 1b: Broader Google search
        broad_url = _search_for_jd_broad(job_title, company)
        if broad_url and broad_url != ats_url:
            log.info("jd_fetcher.google_broad_found", url=broad_url)
            broad_text = _fetch_ats_page(broad_url, timeout)
            if broad_text and len(broad_text) >= min_length and _has_jd_sections(broad_text):
                log.info("jd_fetcher.success", url=broad_url, chars=len(broad_text), source="google_broad")
                return _clean_text(broad_text)
            log.debug("jd_fetcher.google_broad_insufficient", chars=len(broad_text) if broad_text else 0)

    # Step 2: Fall back to hiring.cafe URL via Playwright
    resolved = _resolve_if_sendgrid(url, timeout)
    if resolved:
        log.debug("jd_fetcher.trying_hiring_cafe", url=resolved)
        text, hiring_cafe_ats_url = _fetch_with_playwright(resolved, timeout)

        # Check if hiring.cafe rendered valid content
        if text and len(text) >= min_length and _has_jd_sections(text):
            log.info("jd_fetcher.success", url=resolved, chars=len(text), source="hiring.cafe")
            return _clean_text(text)

        log.debug(
            "jd_fetcher.hiring_cafe_insufficient",
            chars=len(text) if text else 0,
            has_sections=_has_jd_sections(text) if text else False,
            ats_fallback=hiring_cafe_ats_url,
        )

        # Try ATS link found on the hiring.cafe page
        if hiring_cafe_ats_url:
            ats_text = _fetch_ats_page(hiring_cafe_ats_url, timeout)
            if ats_text and len(ats_text) >= min_length and _has_jd_sections(ats_text):
                log.info("jd_fetcher.success", url=hiring_cafe_ats_url, chars=len(ats_text), source="hiring_cafe_ats")
                return _clean_text(ats_text)
    else:
        log.warning("jd_fetcher.resolve_failed", url=url[:80])

    log.warning("jd_fetcher.failed", url=url[:80], job_title=job_title, company=company)
    return None


# ── Google search helpers ─────────────────────────────────────────────────────

# Rate limiting: track last Google search time
_last_google_search: float = 0.0
_GOOGLE_SEARCH_DELAY: float = 2.5  # seconds between Google requests

def _rate_limit_google() -> None:
    """Enforce minimum delay between Google searches."""
    global _last_google_search
    now = time.time()
    elapsed = now - _last_google_search
    if elapsed < _GOOGLE_SEARCH_DELAY:
        time.sleep(_GOOGLE_SEARCH_DELAY - elapsed)
    _last_google_search = time.time()


def _extract_urls_from_html(html: str, filter_domains: list[str] | None = None) -> list[str]:
    """
    Extract real URLs from search results HTML (Google or DuckDuckGo).

    Handles:
      - Google redirect URLs: /url?q=<real_url>
      - DuckDuckGo redirect URLs: //duckduckgo.com/l/?uddg=<encoded_url>
      - Direct href links to target domains
    """
    from urllib.parse import unquote

    urls = []
    seen = set()

    def _add(url: str) -> None:
        if url in seen:
            return
        if filter_domains:
            if any(domain in url for domain in filter_domains):
                seen.add(url)
                urls.append(url)
        else:
            seen.add(url)
            urls.append(url)

    # Pattern 1: Google redirect URLs  /url?q=https://...&sa=...
    for match in re.finditer(r'/url\?q=(https?://[^&"]+)', html):
        _add(unquote(match.group(1)))

    # Pattern 2: DuckDuckGo redirect URLs  //duckduckgo.com/l/?uddg=<encoded_url>&...
    for match in re.finditer(r'//duckduckgo\.com/l/\?uddg=(https?%3A[^&"]+)', html):
        _add(unquote(match.group(1)))

    # Pattern 3: Direct href links
    for match in re.finditer(r'href="(https?://[^"]+)"', html):
        _add(match.group(1))

    return urls


def _web_search(query: str, label: str) -> str | None:
    """
    Execute a web search using DuckDuckGo HTML (no JS required).

    DuckDuckGo is much more permissive than Google for automated queries.
    Falls back to Google via Playwright if DDG fails.
    Returns the page HTML, or None on failure.
    """
    # DuckDuckGo HTML-only endpoint (no JS needed)
    ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    log.debug(f"jd_fetcher.search_{label}", query=query[:120])

    try:
        _rate_limit_google()
        with httpx.Client(
            headers=HEADERS,
            follow_redirects=True,
            timeout=15,
        ) as client:
            resp = client.get(ddg_url)

        if resp.status_code in (200, 202) and len(resp.text) > 500:
            log.debug(f"jd_fetcher.ddg_{label}_ok", chars=len(resp.text), status=resp.status_code)
            return resp.text

        log.debug(f"jd_fetcher.ddg_{label}_bad_status", status=resp.status_code, content_len=len(resp.text))

    except Exception as e:
        log.debug(f"jd_fetcher.ddg_{label}_error", error=str(e))

    # Fallback: Google via Playwright
    google_url = f"https://www.google.com/search?q={quote_plus(query)}&num=10&hl=en"
    try:
        _rate_limit_google()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page = context.new_page()
                try:
                    page.goto(google_url, wait_until="domcontentloaded", timeout=15000)
                except PlaywrightTimeout:
                    return None
                page.wait_for_timeout(1500)
                body_text = page.inner_text("body") or ""
                if "unusual traffic" in body_text.lower() or "captcha" in body_text.lower():
                    log.debug(f"jd_fetcher.google_{label}_captcha")
                    return None
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        log.warning(f"jd_fetcher.google_{label}_error", error=str(e))
        return None


def _search_for_jd(job_title: str, company: str) -> str | None:
    """
    Search for the job posting on known ATS platforms via web search.

    Returns the first matching ATS URL, or None.
    """
    ats_site_query = " OR ".join(f"site:{domain}" for domain in ATS_DOMAINS)
    query = f'"{job_title}" "{company}" {ats_site_query}'

    html = _web_search(query, "ats")
    if html is None:
        return None

    urls = _extract_urls_from_html(html, filter_domains=ATS_DOMAINS)
    if urls:
        log.debug("jd_fetcher.search_ats_results", count=len(urls), first=urls[0][:100])
        return urls[0]

    log.debug("jd_fetcher.search_ats_no_results")
    return None


def _search_for_jd_broad(job_title: str, company: str) -> str | None:
    """
    Broader web search for the job posting on any careers page.

    Returns the first plausible job posting URL, or None.
    """
    query = f'"{job_title}" "{company}" careers apply'

    html = _web_search(query, "broad")
    if html is None:
        return None

    # Look for ATS domains first, then any job-looking URL
    urls = _extract_urls_from_html(html, filter_domains=ATS_DOMAINS)
    if urls:
        return urls[0]

    # Broader: look for URLs with job-related path segments
    all_urls = _extract_urls_from_html(html)
    job_patterns = ["/jobs/", "/careers/", "/job/", "/position/", "/apply/", "/opening/"]
    for url in all_urls:
        # Skip search engine URLs and other noise
        if "google.com" in url or "youtube.com" in url or "duckduckgo.com" in url:
            continue
        if any(pat in url.lower() for pat in job_patterns):
            return url

    log.debug("jd_fetcher.search_broad_no_results")
    return None


# ── Internal helpers ──────────────────────────────────────────────────────────


def _resolve_if_sendgrid(url: str, timeout: int) -> str | None:
    """Follow SendGrid redirects; pass through if already a direct URL."""
    if "sendgrid.net" in url:
        return resolve_sendgrid_url(url, timeout=timeout)
    return url


def _fetch_with_playwright(url: str, timeout: int) -> tuple[str | None, str | None]:
    """
    Load a hiring.cafe job page with headless Chromium.

    Returns (jd_text, ats_apply_url).  Both can be None.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page = context.new_page()
                page.set_extra_http_headers({
                    "Accept": HEADERS["Accept"],
                    "Accept-Language": HEADERS["Accept-Language"],
                })

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except PlaywrightTimeout:
                    log.warning("jd_fetcher.playwright_goto_timeout", url=url)
                    # Continue — page may be partially loaded and still usable

                # Give the bot check time to clear and JS content to render
                page.wait_for_timeout(3000)

                # Try to wait for substantive content to appear in the DOM
                for selector in ["main", "article", "[class*='description']", "h1"]:
                    try:
                        page.wait_for_selector(selector, timeout=5000)
                        break
                    except PlaywrightTimeout:
                        continue

                text = _extract_best_text(page)
                ats_url = _find_ats_link(page)
                return text, ats_url
            finally:
                browser.close()

    except Exception as e:
        log.warning("jd_fetcher.playwright_error", error=str(e), url=url)
        return None, None


def _extract_best_text(page) -> str | None:
    """
    Find and return the richest JD text block on the rendered page.

    Walks selectors from most specific to least; falls back to full body text.
    """
    selectors = [
        # hiring.cafe / Next.js component class patterns
        "[class*='JobDescription']",
        "[class*='job-description']",
        "[class*='jobDescription']",
        "[class*='job_description']",
        "[class*='jd-content']",
        "[class*='posting-description']",
        "[class*='job-details']",
        "[data-testid*='description']",
        # Tailwind prose wrapper (common in Next.js apps)
        ".prose",
        "[class*='prose']",
        # Generic semantic containers
        "article",
        "main",
        "[role='main']",
    ]

    best_text = None
    best_len = 0

    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if not el:
                continue
            text = el.inner_text()
            if not text:
                continue
            text = text.strip()
            if len(text) > best_len:
                best_text = text
                best_len = len(text)
                # Short-circuit: confident JD with real section headers
                if best_len > 500 and _has_jd_sections(text):
                    return best_text
        except Exception:
            continue

    if best_text and best_len > 100:
        return best_text

    # Last resort: full page body text
    try:
        return page.inner_text("body")
    except Exception:
        return None


def _find_ats_link(page) -> str | None:
    """
    Look for an external ATS link on the hiring.cafe page.

    Prefers links pointing to known ATS domains, then falls back to any
    off-site "Apply" anchor.
    """
    try:
        links = page.query_selector_all("a[href]")
        ats_candidates = []

        for link in links:
            href = link.get_attribute("href") or ""
            if not href.startswith("http"):
                continue

            if any(domain in href for domain in ATS_DOMAINS):
                ats_candidates.append(href)
                continue

            link_text = (link.inner_text() or "").lower().strip()
            if "apply" in link_text and "hiring.cafe" not in href:
                ats_candidates.append(href)

        return ats_candidates[0] if ats_candidates else None

    except Exception:
        return None


def _fetch_ats_page(url: str, timeout: int) -> str | None:
    """
    Fetch JD content from an external ATS page.

    Tries httpx + readability first (fast, works for most ATS).
    Falls back to Playwright for JS-heavy pages.
    """
    try:
        with httpx.Client(
            headers=HEADERS,
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            resp = client.get(url)

        if resp.status_code == 200:
            from readability import Document
            from bs4 import BeautifulSoup

            doc = Document(resp.text)
            soup = BeautifulSoup(doc.summary(), "lxml")
            text = soup.get_text(separator="\n", strip=True)
            if text and len(text) > 200:
                log.debug("jd_fetcher.ats_httpx_ok", url=url, chars=len(text))
                return text
        else:
            log.debug("jd_fetcher.ats_bad_status", status=resp.status_code, url=url)

    except Exception as e:
        log.debug("jd_fetcher.ats_httpx_error", error=str(e), url=url)

    # Playwright fallback for ATS pages with their own bot protection
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(2000)
                text = page.inner_text("body")
                return text
            finally:
                browser.close()

    except Exception as e:
        log.debug("jd_fetcher.ats_playwright_error", error=str(e), url=url)
        return None


def _has_jd_sections(text: str) -> bool:
    """True if text contains at least one recognizable JD section header."""
    return any(re.search(pat, text) for pat in JD_SECTION_PATTERNS)


def _clean_text(text: str) -> str:
    """Collapse excess blank lines and trim boilerplate footers."""
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Strip common boilerplate phrases if they appear near the end (>70% in)
    for phrase in [
        "Equal Opportunity Employer",
        "Submit your application",
        "Apply for this job",
        "Apply now",
    ]:
        idx = text.lower().rfind(phrase.lower())
        if idx > 0 and idx > len(text) * 0.7:
            text = text[:idx].rstrip()

    return text.strip()
