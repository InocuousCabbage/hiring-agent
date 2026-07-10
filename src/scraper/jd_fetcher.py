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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx
import structlog
from urllib.parse import quote_plus
from playwright.sync_api import Browser, sync_playwright, TimeoutError as PlaywrightTimeout

from parser.email_parser import resolve_sendgrid_url

log = structlog.get_logger()


@dataclass(frozen=True)
class JDFetchResult:
    """
    Job-description fetch result.

    text          — cleaned JD text (always populated on success)
    ats_apply_url — discovered ATS "Apply Now" URL (Greenhouse, Lever, Ashby,
                    Workday, iCIMS, SmartRecruiters, etc.) or None if the JD
                    came from a non-ATS source (e.g. pure hiring.cafe).
    ats           — canonical ATS name matching ats_apply_url, or None.

    Consumed by Phase 3 auto-apply to route submissions to the correct ATS
    endpoint instead of the SendGrid tracking URL in job['url'].
    """
    text: str
    ats_apply_url: str | None = None
    ats: str | None = None

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

# Known ATS domains where external job postings live, mapped to their canonical
# vendor names. Order matters — first match wins in _infer_ats_name, so more
# specific patterns (e.g. myworkdayjobs.com before ashbyhq.com) are fine here
# because domains are non-overlapping.
_ATS_DOMAIN_TO_NAME: dict[str, str] = {
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "myworkdayjobs.com": "Workday",
    "icims.com": "iCIMS",
    "brassring.com": "BrassRing",
    "smartrecruiters.com": "SmartRecruiters",
    "jobvite.com": "Jobvite",
    "taleo.net": "Taleo",
    "successfactors.com": "SuccessFactors",
    "bamboohr.com": "BambooHR",
    "ashbyhq.com": "Ashby",
    "rippling.com": "Rippling",
}
# Preserved as a list for existing filter_domains callers.
ATS_DOMAINS: list[str] = list(_ATS_DOMAIN_TO_NAME.keys())


def _infer_ats_name(url: str | None) -> str | None:
    """
    Map an ATS apply URL to its canonical vendor name.

    Returns None when the URL is None or not from a recognized ATS. Used by
    fetch_job_description to populate JDFetchResult.ats alongside the URL.
    """
    if not url:
        return None
    for domain, name in _ATS_DOMAIN_TO_NAME.items():
        if domain in url:
            return name
    return None


@contextmanager
def _browser_or_launch(browser: Browser | None) -> Iterator[Browser]:
    """Yield a Browser: use ``browser`` if provided, else launch + tear down.

    H15 (Phase 6 audit): call sites use::

        with _browser_or_launch(shared_browser) as b:
            context = b.new_context(...)
            ...

    When ``shared_browser`` is not None, this is a no-op passthrough so
    the caller's shared instance is used and its lifecycle is not touched.
    When None, we do the historical per-call ``sync_playwright()`` +
    ``chromium.launch()`` + ``browser.close()`` inside a wrapping
    try/finally so the callers stay backward-compatible.
    """
    if browser is not None:
        yield browser
        return
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def fetch_job_description(
    url: str,
    timeout: int = 30,
    min_length: int = 200,
    job_title: str = "",
    company: str = "",
    browser: Browser | None = None,
) -> JDFetchResult | None:
    """
    Fetch and return a JDFetchResult for a job URL, or None on failure.

    The returned dataclass carries:
      - text          : cleaned JD content
      - ats_apply_url : discovered ATS "Apply Now" URL (Greenhouse, Lever,
                        Ashby, Workday, iCIMS, SmartRecruiters, ...) or None
      - ats           : canonical ATS vendor name or None

    Downstream code (Phase 3 auto-apply) uses ats_apply_url to route form
    submissions to the correct ATS endpoint rather than falling back to the
    SendGrid tracking URL in job['url'].

    Strategy (in order):
      1. Google search for ATS posting using job_title + company (populates
         ats_apply_url when successful)
      2. Broader Google search — if the hit is on an ATS domain it also
         populates ats_apply_url
      3. Fall back to hiring.cafe URL via Playwright — its outbound "Apply"
         link, if any, populates ats_apply_url when we use it as the fetch
         source
      4. Return None if everything fails

    Returns None if:
      - No ATS posting found via search AND hiring.cafe is blocked
      - Extracted text is shorter than min_length
      - No recognizable JD section headers found
    """
    # Step 1: Try Google search for direct ATS posting
    if job_title and company:
        ats_url = _search_for_jd(job_title, company, browser=browser)
        if ats_url:
            log.info("jd_fetcher.google_found_ats", url=ats_url)
            ats_text = _fetch_ats_page(ats_url, timeout, browser=browser)
            if ats_text and len(ats_text) >= min_length and _has_jd_sections(ats_text):
                log.info("jd_fetcher.success", url=ats_url, chars=len(ats_text), source="google_ats")
                return JDFetchResult(
                    text=_clean_text(ats_text),
                    ats_apply_url=ats_url,
                    ats=_infer_ats_name(ats_url),
                )
            log.debug("jd_fetcher.google_ats_insufficient", chars=len(ats_text) if ats_text else 0)

        # Step 1b: Broader Google search
        broad_url = _search_for_jd_broad(job_title, company, browser=browser)
        if broad_url and broad_url != ats_url:
            log.info("jd_fetcher.google_broad_found", url=broad_url)
            broad_text = _fetch_ats_page(broad_url, timeout, browser=browser)
            if broad_text and len(broad_text) >= min_length and _has_jd_sections(broad_text):
                log.info("jd_fetcher.success", url=broad_url, chars=len(broad_text), source="google_broad")
                # broad_url is an ATS URL only when _infer_ats_name recognizes
                # the domain — a company careers page returns ats=None so
                # Phase 3 knows it can't auto-apply through a vendor form.
                inferred = _infer_ats_name(broad_url)
                return JDFetchResult(
                    text=_clean_text(broad_text),
                    ats_apply_url=broad_url if inferred else None,
                    ats=inferred,
                )
            log.debug("jd_fetcher.google_broad_insufficient", chars=len(broad_text) if broad_text else 0)

    # Step 2: Fall back to hiring.cafe URL via Playwright
    resolved = _resolve_if_sendgrid(url, timeout)
    if resolved:
        log.debug("jd_fetcher.trying_hiring_cafe", url=resolved)
        text, hiring_cafe_ats_url = _fetch_with_playwright(resolved, timeout, browser=browser)

        # Check if hiring.cafe rendered valid content
        if text and len(text) >= min_length and _has_jd_sections(text):
            log.info("jd_fetcher.success", url=resolved, chars=len(text), source="hiring.cafe")
            # If the hiring.cafe page carried an outbound ATS link we surface
            # it even when the JD text itself came from hiring.cafe — Phase 3
            # still wants the ATS submission target when available.
            inferred = _infer_ats_name(hiring_cafe_ats_url)
            return JDFetchResult(
                text=_clean_text(text),
                ats_apply_url=hiring_cafe_ats_url if inferred else None,
                ats=inferred,
            )

        log.debug(
            "jd_fetcher.hiring_cafe_insufficient",
            chars=len(text) if text else 0,
            has_sections=_has_jd_sections(text) if text else False,
            ats_fallback=hiring_cafe_ats_url,
        )

        # Try ATS link found on the hiring.cafe page
        if hiring_cafe_ats_url:
            ats_text = _fetch_ats_page(hiring_cafe_ats_url, timeout, browser=browser)
            if ats_text and len(ats_text) >= min_length and _has_jd_sections(ats_text):
                log.info("jd_fetcher.success", url=hiring_cafe_ats_url, chars=len(ats_text), source="hiring_cafe_ats")
                # _find_ats_link may return a non-ATS "Apply" URL (any off-site
                # anchor whose text contains "apply", see line 437). Mirror the
                # guard used in the google_broad and pure-hiring.cafe paths so
                # Phase 3 auto-apply never sees ats_apply_url set with ats=None
                # (a URL with no vendor is unroutable).
                inferred = _infer_ats_name(hiring_cafe_ats_url)
                return JDFetchResult(
                    text=_clean_text(ats_text),
                    ats_apply_url=hiring_cafe_ats_url if inferred else None,
                    ats=inferred,
                )
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


def _web_search(query: str, label: str, browser: Browser | None = None) -> str | None:
    """
    Execute a web search using DuckDuckGo HTML (no JS required).

    DuckDuckGo is much more permissive than Google for automated queries.
    Falls back to Google via Playwright if DDG fails.
    Returns the page HTML, or None on failure.

    H15 (Phase 6 audit): ``browser``, when provided, is reused for the
    Google Playwright fallback instead of launching a fresh Chromium.
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
        with _browser_or_launch(browser) as b:
            context = b.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            try:
                page = context.new_page()
                try:
                    page.goto(google_url, wait_until="domcontentloaded", timeout=15000)
                except PlaywrightTimeout:
                    return None
                # M16 (Phase 6 audit): the 1500ms unconditional sleep is
                # replaced by a bounded wait for #search (Google's results
                # container) — races through the moment the DOM is
                # queryable and gives up quickly on captcha/interstitial.
                try:
                    page.wait_for_selector("#search, form[action*='sorry']",
                                           state="attached", timeout=3000)
                except PlaywrightTimeout:
                    pass
                body_text = page.inner_text("body") or ""
                if "unusual traffic" in body_text.lower() or "captcha" in body_text.lower():
                    log.debug(f"jd_fetcher.google_{label}_captcha")
                    return None
                return page.content()
            finally:
                context.close()
    except Exception as e:
        log.warning(f"jd_fetcher.google_{label}_error", error=str(e))
        return None


def _search_for_jd(job_title: str, company: str, browser: Browser | None = None) -> str | None:
    """
    Search for the job posting on known ATS platforms via web search.

    Returns the first matching ATS URL, or None.
    """
    ats_site_query = " OR ".join(f"site:{domain}" for domain in ATS_DOMAINS)
    query = f'"{job_title}" "{company}" {ats_site_query}'

    html = _web_search(query, "ats", browser=browser)
    if html is None:
        return None

    urls = _extract_urls_from_html(html, filter_domains=ATS_DOMAINS)
    if urls:
        log.debug("jd_fetcher.search_ats_results", count=len(urls), first=urls[0][:100])
        return urls[0]

    log.debug("jd_fetcher.search_ats_no_results")
    return None


def _search_for_jd_broad(job_title: str, company: str, browser: Browser | None = None) -> str | None:
    """
    Broader web search for the job posting on any careers page.

    Returns the first plausible job posting URL, or None.
    """
    query = f'"{job_title}" "{company}" careers apply'

    html = _web_search(query, "broad", browser=browser)
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


def _fetch_with_playwright(url: str, timeout: int, browser: Browser | None = None) -> tuple[str | None, str | None]:
    """
    Load a hiring.cafe job page with headless Chromium.

    Returns (jd_text, ats_apply_url).  Both can be None.

    H15 (Phase 6 audit): when ``browser`` is provided, we reuse it and
    only allocate a per-fetch ``BrowserContext`` — that skips the ~2-4s
    Chromium startup that pre-fix paid per call.

    M16 (Phase 6 audit): pre-fix, every fetch paid a hard 3000ms sleep
    followed by up to four sequential 5000ms selector waits (≥3s floor,
    ~23s worst-case). Post-fix: one comma-separated CSS ``wait_for_selector``
    call races all four candidate selectors in a single wait — early-return
    on the first hit, no unconditional sleep.
    """
    try:
        with _browser_or_launch(browser) as b:
            context = b.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            try:
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

                # M16: race all substantive-content selectors in ONE wait.
                # Comma-separated CSS gives an OR — the first match releases
                # us. Cap total wait at 5s (matches the pre-fix per-selector
                # cap, but the pre-fix loop paid up to 20s). Instantly-rendered
                # pages return in ms.
                try:
                    page.wait_for_selector(
                        "main, article, [class*='description'], h1",
                        timeout=5000,
                    )
                except PlaywrightTimeout:
                    # No substantive content within 5s — extract_best_text
                    # will still try to pull body text; a captcha/interstitial
                    # will fall through to None + a downstream retry.
                    pass

                text = _extract_best_text(page)
                ats_url = _find_ats_link(page)
                return text, ats_url
            finally:
                context.close()

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

    M15 (Phase 6 audit): the anchor scan is now a single ``eval_on_selector_all``
    JS eval — the browser returns all (href, text) pairs in one CDP
    round-trip. Pre-fix, this looped over every anchor with 2 sync
    round-trips each (``get_attribute("href")`` + ``inner_text()``), so a
    hiring.cafe page with 200+ anchors paid 400+ sequential IPCs. All
    filtering runs in Python on the returned list.
    """
    try:
        # Return only href + trimmed text; the trim/lowercase steps stay
        # in Python for symmetry with the pre-fix filter and to keep the
        # JS payload small enough for hostile pages that stub text APIs.
        raw = page.eval_on_selector_all(
            "a[href]",
            "elements => elements.map(el => ({href: el.getAttribute('href') || '', text: (el.textContent || '').trim()}))",
        )
    except Exception:
        return None

    for entry in raw or []:
        href = entry.get("href") if isinstance(entry, dict) else ""
        text = entry.get("text") if isinstance(entry, dict) else ""
        if not href or not href.startswith("http"):
            continue
        if any(domain in href for domain in ATS_DOMAINS):
            return href
        # Non-ATS off-site anchor: only counts if the visible label contains
        # "apply". Preserves the pre-fix precedence (first match wins).
        text_lower = (text or "").lower()
        if "apply" in text_lower and "hiring.cafe" not in href:
            return href

    return None


def _fetch_ats_page(url: str, timeout: int, browser: Browser | None = None) -> str | None:
    """
    Fetch JD content from an external ATS page.

    Tries httpx + readability first (fast, works for most ATS).
    Falls back to Playwright for JS-heavy pages.

    H15 (Phase 6 audit): when ``browser`` is provided, the Playwright
    fallback reuses it instead of launching Chromium per call.

    M16 (Phase 6 audit): the pre-fix unconditional 2-second sleep after
    goto is dropped — ``networkidle`` (or its timeout) is already a
    substantive wait, and the body-text extraction is deterministic
    against the loaded DOM.
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
        with _browser_or_launch(browser) as b:
            context = b.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            try:
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                except PlaywrightTimeout:
                    pass
                # M16: no unconditional 2000ms sleep — networkidle (or
                # its timeout above) already gates on network activity
                # settling, so body-text extraction is stable.
                text = page.inner_text("body")
                return text
            finally:
                context.close()

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
