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
import unicodedata
from email import policy
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import httpx
import structlog

log = structlog.get_logger()


# Phase 5 iter-3 CRITICAL #5 (altitude fix, iter-1 + iter-2 + iter-3 review
# hardening):
#
# `str.strip()` only removes chars where `str.isspace()` is True. The Unicode
# Cf category (format characters — ZWSP U+200B, BOM U+FEFF, LRM/RLM U+200E/F,
# WJ U+2060, bidi override/isolate U+202A-U+202E and U+2066-U+2069, SOFT
# HYPHEN U+00AD, ALM U+061C, MVS U+180E, and more) mostly has `isspace()=False`
# — so `str.strip()` leaves them behind. The Cc category (control chars — NUL
# U+0000, BEL U+0007, etc.) includes non-whitespace controls that also survive
# `.strip()`. A hostile hiring.cafe alert whose company div raw text begins
# with e.g. `"​— Remote"` (ZWSP-prefixed) OR `"‮— Remote"` (RLO-prefixed) OR
# `"­— Remote"` (SHY-prefixed) would produce `parts[0].strip() = <invisible>`
# — truthy (length 1), so the `or "Unknown"` fallback is bypassed and the
# invisible sentinel leaks downstream to the exact renderer filename collision
# + LLM prompt corruption the altitude fix was meant to eliminate.
#
# Policy — denylist inversion (iter-3 security + correctness reviewer
# consensus): strip the ENTIRE Cf category EXCEPT the two semantically
# load-bearing script/emoji chars — ZWJ (U+200D) and ZWNJ (U+200C). This is
# robust to future Unicode additions of new invisible-sentinel chars, whereas
# iter-2's allowlist approach silently let 12 known-hostile Cf codepoints
# through (verified end-to-end M7 silent-drop reproduction with U+202E RLO
# and U+00AD SHY).
#
# ZWJ/ZWNJ preservation matters because they're required for:
#   - Persian/Urdu word segmentation (e.g. "دیجی‌کالا" / Digikala uses ZWNJ)
#   - Devanagari/Bengali/Tamil/Malayalam conjunct rendering (ZWJ)
#   - Multi-codepoint emoji sequences (ZWJ glues 👨‍👩‍👧‍👦 family emoji, etc.)
# Blanket Cf strip would silently corrupt these; the two-char denylist
# preserves them without opening a hostile-invisible bypass.
_CF_PRESERVE = frozenset({"‌", "‍"})  # ZWNJ, ZWJ


def _strip_format_chars(s: str) -> str:
    """Strip invisible chars that `str.strip()` alone does not touch,
    to prevent a truthy invisible sentinel from bypassing the extraction
    fallback.

    Removes:
      - All Cf (format) characters EXCEPT ZWJ (U+200D) and ZWNJ (U+200C).
        This denylist-inverted policy covers ZWSP, LRM/RLM, WJ, BOM, bidi
        override/isolate (U+202A-U+202E, U+2066-U+2069), SOFT HYPHEN, ALM,
        MVS, and any future Unicode additions to Cf.
      - Cc (control) characters that are NOT `isspace()` — NUL, BEL, and
        other C0/C1 controls a hostile source could smuggle. `str.strip()`
        already handles isspace Cc chars like `\\n`/`\\t`, so those are not
        stripped here.

    Preserves:
      - ZWJ / ZWNJ (load-bearing in Persian/Urdu/Indic scripts and
        multi-codepoint emojis).
      - All non-Cf, non-Cc chars.
    """
    return "".join(
        ch for ch in s
        if not (unicodedata.category(ch) == "Cf" and ch not in _CF_PRESERVE)
        and not (unicodedata.category(ch) == "Cc" and not ch.isspace())
    )


def _has_meaningful_content(s: str) -> bool:
    """True iff `s` contains at least one non-Cf, non-Cc, non-whitespace char.

    Iter-4 correctness reviewer surfaced a residual M7 silent-drop attack
    on the two Cf chars we preserve (ZWJ U+200D and ZWNJ U+200C): a raw
    div like `"‍— Remote"` (ZWJ-prefixed) leaves `parts[0].strip() = "‍"`
    — truthy, non-whitespace, preserved by `_strip_format_chars` (which
    correctly avoids stripping ZWJ from legitimate script/emoji use) —
    so the `or "Unknown"` fallback is bypassed and the invisible sentinel
    leaks downstream. Two same-title cards with ZWJ-prefixed raws would
    dedup on `(title, "‍")` and silently drop the second — the exact
    M7 class this fix targets, on a 2-char attack surface.

    The extraction site distinguishes "no real content" from "real content
    that CONTAINS ZWJ/ZWNJ mid-string" by asking whether ANY char is
    non-Cf and non-Cc and non-whitespace. `"‍"` (ZWJ alone) → False →
    fall to "Unknown". `"دیجی‌کالا"` (real Persian name with ZWNJ
    mid-word) → True → preserved verbatim. `"Legit Corp"` → True.

    The Cc exclusion is defense-in-depth: `_strip_format_chars` already
    removes non-isspace Cc chars upstream, but keeping the predicate
    self-contained means a future refactor that inlines or reorders the
    strip cannot silently reintroduce a Cc-only bypass.
    """
    return any(
        unicodedata.category(ch) not in ("Cf", "Cc") and not ch.isspace()
        for ch in s
    )


def _split_company_location(raw: str, delim: str) -> tuple[str, str]:
    """Split the company/location raw div text on `delim` (em-dash or
    en-dash), returning `(company, location)`.

    Callers pass one of the two dash delimiters, gated on `delim in raw`,
    so the split always yields two parts.

    Applies the altitude-fix invariant on the company side: no empty,
    whitespace-shaped, or invisible-only value ever leaves this function.
    `_has_meaningful_content` rejects strings that are only ZWJ/ZWNJ —
    both of which `_strip_format_chars` preserves for legitimate script
    and emoji use — so a leading-ZWJ hostile raw falls through to the
    "Unknown" fallback the same as an empty result.

    `location` is intentionally NOT normalized — an empty string
    (trailing-dash raw like `"Acme —"`) is a downstream cosmetic concern,
    not the M7 cascade class the altitude fix targets, and coercing to
    `None` regresses callers that use `.get('location', 'Not specified')`
    (which then renders literal `"None"`). Tracked to PR #12.
    """
    parts = raw.split(delim, 1)
    company_raw = parts[0].strip()
    company = company_raw if _has_meaningful_content(company_raw) else "Unknown"
    location = parts[1].strip()
    return company, location


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

        # Title participates in the (title, company) dedup key, so the
        # same invisible-sentinel hardening the company field carries
        # (iter-4 correctness reviewer): a hostile raw with three ZWNJ
        # chars would pass `len(title) < 3` and propagate to the LLM
        # prompt as `Title: ‌‌‌`, corrupting the cover-letter output.
        # Sweep + meaningful-content check catches the class.
        title = _strip_format_chars(title).strip()

        if not title or len(title) < 3 or not _has_meaningful_content(title):
            continue

        # Navigate to the parent <td> which contains the full card
        card = h3.find_parent("td")
        if not card:
            continue

        # Extract company + location from first <div> after h3.
        #
        # Phase 5 iter-3 CRITICAL #5 (altitude fix): normalize the company
        # value at parser extraction so no empty/invisible-shaped sentinel
        # leaves this function. Downstream consumers of `job['company']` —
        # renderer filename (pdf_gen/renderer.py), LLM prompts (tailor/
        # cover_letter.py + contacts/hm_finder.py), notify subject + body
        # (apply/review.py + apply/notify.py) — each read from this single
        # choke point; per-site test rationale lives with the tests in
        # tests/test_review_fixes.py (TestEmailParserCompanyNormalization*).
        #
        # Three hardening layers:
        #   1. `_strip_format_chars(raw)` strips ALL Cf (format) chars
        #      except ZWJ/ZWNJ, plus non-isspace Cc controls. Denylist-
        #      inverted policy (see the `_CF_PRESERVE` block above) —
        #      robust to future Unicode additions of new invisible
        #      sentinels (bidi override/isolate, SOFT HYPHEN, ALM, MVS,
        #      etc.) that an allowlist would silently pass.
        #   2. `_split_company_location` applies the `or "Unknown"`
        #      fallback via `_has_meaningful_content`, which rejects
        #      strings composed only of the preserved ZWJ/ZWNJ chars
        #      (iter-4 residual attack surface).
        #   3. The no-dash-fallback branch strips `raw` once before the
        #      meaningful-content check + assignment. `str.strip()` is
        #      needed here for the assignment (not the predicate — the
        #      predicate is whitespace-invariant) because the Cf sweep
        #      can expose interior whitespace that was previously bounded
        #      by Cf chars, and we want the assigned company value clean
        #      of leading/trailing whitespace. Verified against real BS4
        #      output.
        company = "Unknown"
        location = None
        company_div = h3.find_next_sibling("div")
        if company_div:
            raw = _strip_format_chars(company_div.get_text(strip=True))
            if "—" in raw:
                company, location = _split_company_location(raw, "—")
            elif "–" in raw:
                company, location = _split_company_location(raw, "–")
            else:
                raw_stripped = raw.strip()
                if _has_meaningful_content(raw_stripped):
                    company = raw_stripped

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

        # M7 + Phase 5 iter-2/iter-3: build the dedup key AFTER url is
        # validated. When the parser could not identify a real company,
        # key on URL instead so distinct-URL jobs both surface.
        #
        # PR #12 iter-3 (contrarian + sweep review consensus): `company`
        # is stripped upstream on every branch — `_split_company_location`
        # returns `parts[0].strip()` post `_has_meaningful_content` gate,
        # and the `elif` fallback stores `raw_stripped` post the same
        # gate. `not company` alone catches the empty-string case;
        # `not company.strip()` disjunct was proven dead (no producer
        # path yields whitespace-shaped truthy company). Removed to keep
        # the invariant anchored at the extraction block rather than
        # scattered defensive OR-chains downstream.
        no_real_company = (company == "Unknown") or (not company)
        dedup_key = (title, url) if no_real_company else (title, company)
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
