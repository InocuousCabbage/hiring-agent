"""
parser/link_submission.py — HALF 2 intake: parse a user-submitted email that
lists hiring.cafe ``/job/...`` links and turn each link into a pipeline-shaped
job dict.

This is the SECOND Gmail intake path (distinct from the ali@hiring.cafe alert
parsed by ``parser.email_parser``). A user emails the agent a batch of
``https://hiring.cafe/job/{slug}-{id}`` links; ``extract_job_links`` pulls the
links out of the message body and ``build_jobs_from_links`` produces job dicts
that match ``email_parser.parse_alert_email``'s shape EXACTLY, so the existing
``main.run_pipeline`` consumes them verbatim.

Design note (plan R4, option a — slug-derived title/company + JSON-driven JD):
at build time we only hold the URL, so ``title`` is derived cheaply from the
slug and ``company`` falls to the ``"Unknown"`` sentinel (an undelimited slug
cannot be reliably segmented into title vs company vs location, and fabricating
a boundary would corrupt BOTH fields). The AUTHORITATIVE JD still arrives inside
``run_pipeline``'s ``fetch_job_description`` via the HALF 1 ``#__NEXT_DATA__``
path — the slug values are placeholders good enough for logging + the renderer
filename (accepted as slug-cased per plan R4). Avoiding a build-time page load
keeps the intake to one fetch per job (option a, not b).
"""
import re

from bs4 import BeautifulSoup
import structlog

# Reuse the HALF 1 host+/job/ recognizer so link validation matches the fetch
# path exactly (it already rejects legacy ``/viewjob/`` links, whose path does
# not start with ``/job/``).
from scraper.jd_fetcher import _is_hiringcafe_job_url

# Reuse the alert parser's invisible-sentinel hardening so a hostile slug can
# never leak an empty/invisible-only title downstream (plan Non-negotiable #1).
from parser.email_parser import _strip_format_chars, _has_meaningful_content

log = structlog.get_logger()

# Match a hiring.cafe /job link on either host. The trailing character class
# stops at the first non-slug char (whitespace, quote, angle bracket, etc.),
# so a link embedded in prose or an <a href> is captured cleanly.
_JOB_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:hiring\.cafe|hiringcafe\.com)/job/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# Default per-submission cap (plan R7 — start at 10, raise after a live cost
# read). A single job runs >=2 sonnet calls + QA + a haiku HM lookup + one page
# fetch, so the ceiling is a real cost/latency guard, not a formality.
_DEFAULT_MAX_PER_SUBMISSION = 10

_UNKNOWN_COMPANY = "Unknown"


def extract_job_links(text_body: str, html_body: str = "") -> list[str]:
    """Pull hiring.cafe ``/job/...`` links out of a submission email.

    Scans the plain-text body first (regex), then the HTML body — both its
    ``<a href>`` targets and any links sitting in HTML text nodes — as a
    fallback. Non-hiring.cafe links and legacy ``/viewjob/`` links are ignored
    (the latter via ``_is_hiringcafe_job_url``, which only accepts ``/job/``
    paths). Order of first appearance is preserved and duplicates are dropped.
    """
    candidates: list[str] = []

    if text_body:
        candidates.extend(_JOB_LINK_RE.findall(text_body))

    if html_body:
        soup = BeautifulSoup(html_body, "lxml")
        for anchor in soup.find_all("a", href=True):
            candidates.append(anchor["href"].strip())
        # Links can also live in visible HTML text (pasted, not hyperlinked).
        candidates.extend(_JOB_LINK_RE.findall(soup.get_text(" ")))

    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        url = url.strip()
        if not _is_hiringcafe_job_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)

    log.info("link_submission.extracted", link_count=len(out))
    return out


def _slug_from_url(url: str) -> str:
    """Return the final path segment after ``/job/`` (the ``{slug}-{id}``)."""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.rsplit("/job/", 1)[-1].rsplit("/", 1)[-1]


def _looks_like_id_token(token: str) -> bool:
    """A hiring.cafe id token is a hyphen segment mixing letters + digits,
    e.g. ``zmxdntl7i86xpys3`` / ``abc123def456``. Used to drop the id tail from
    the human-readable slug so it doesn't leak into the derived title."""
    return (
        len(token) >= 6
        and token.isalnum()
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )


def _slug_to_title(url: str) -> str:
    """Derive a human-ish title from the URL slug.

    Splits the slug on ``-``, drops a trailing id-like token, and title-cases
    the remaining words. Deterministic and lossy by design (see module doc):
    the authoritative title/company/JD come from the page JSON at fetch time.
    """
    slug = _slug_from_url(url)
    parts = [p for p in slug.split("-") if p]
    if parts and _looks_like_id_token(parts[-1]):
        parts = parts[:-1]
    return " ".join(word.capitalize() for word in parts)


def build_jobs_from_links(links, config, browser=None) -> list[dict]:
    """Build pipeline-shaped job dicts from hiring.cafe ``/job`` links.

    Each dict matches ``email_parser.parse_alert_email``'s 8-key shape exactly
    (``title, company, location, date_posted, salary, description_snippet, url,
    resolved_url``) so ``run_pipeline``'s ``job["title"]/["company"]/["url"]``
    reads never KeyError. ``title`` is slug-derived; ``company`` is the
    ``"Unknown"`` sentinel (mirroring the alert parser) because the slug cannot
    be reliably segmented; the real JD arrives via ``fetch_job_description``.

    Non-``/job`` links are skipped. The batch is capped at
    ``jobs.max_per_submission`` (default 10). ``browser`` is accepted for
    signature parity with option (b); option (a) needs no build-time page load.
    """
    cap = _DEFAULT_MAX_PER_SUBMISSION
    if isinstance(config, dict):
        raw_cap = (config.get("jobs") or {}).get("max_per_submission")
        if isinstance(raw_cap, int) and not isinstance(raw_cap, bool) and raw_cap > 0:
            cap = raw_cap

    jobs: list[dict] = []
    for url in links:
        url = (url or "").strip()
        if not _is_hiringcafe_job_url(url):
            continue

        title = _strip_format_chars(_slug_to_title(url)).strip()
        if not _has_meaningful_content(title):
            # A slug that yields no meaningful title (all id/empty) still gets
            # a stable, non-empty placeholder so downstream reads never break.
            title = "Job Posting"

        jobs.append({
            "title": title,
            "company": _UNKNOWN_COMPANY,
            "location": None,
            "date_posted": None,
            "salary": None,
            "description_snippet": None,
            "url": url,
            "resolved_url": url,  # already a direct, resolved hiring.cafe URL
        })
        if len(jobs) >= cap:
            break

    log.info("link_submission.built_jobs", job_count=len(jobs), cap=cap)
    return jobs
