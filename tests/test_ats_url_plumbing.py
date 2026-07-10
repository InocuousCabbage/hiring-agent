"""
tests/test_ats_url_plumbing.py — ATS URL plumbing for downstream auto-apply.

Verifies that scraper.jd_fetcher.fetch_job_description surfaces the discovered
ATS apply URL (Greenhouse, Lever, Ashby, Workday, iCIMS, etc.) so Phase 3
auto-apply can route to the right submission endpoint instead of falling back
to the SendGrid tracking URL in job['url'].

Covers:
  - _infer_ats_name maps known ATS URLs to canonical names
  - _infer_ats_name returns None for non-ATS URLs
  - fetch_job_description returns a JDFetchResult carrying ats_apply_url + ats
    when the google_ats path discovers an ATS posting
  - fetch_job_description returns a JDFetchResult carrying ats_apply_url + ats
    from _find_ats_link when the hiring.cafe fallback discovers an ATS link
  - fetch_job_description returns ats_apply_url=None + ats=None when no ATS
    was discovered (backward-compatible failure mode for Phase 3)
  - _find_ats_link (unchanged interface) still finds ATS URLs from a page
    with a Greenhouse "Apply Now" anchor
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from scraper import jd_fetcher
from scraper.jd_fetcher import (
    JDFetchResult,
    _find_ats_link,
    _infer_ats_name,
    fetch_job_description,
)


# ── _infer_ats_name unit tests ────────────────────────────────────────────────

class TestInferAtsName:
    def test_greenhouse(self):
        assert _infer_ats_name("https://boards.greenhouse.io/acme/jobs/12345") == "Greenhouse"

    def test_lever(self):
        assert _infer_ats_name("https://jobs.lever.co/acme/abc-def") == "Lever"

    def test_ashby(self):
        assert _infer_ats_name("https://jobs.ashbyhq.com/acme/uuid") == "Ashby"

    def test_workday(self):
        assert _infer_ats_name(
            "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Marketing_Ops_R123"
        ) == "Workday"

    def test_icims(self):
        assert _infer_ats_name("https://careers-acme.icims.com/jobs/1234/marketing-ops/job") == "iCIMS"

    def test_smartrecruiters(self):
        assert _infer_ats_name("https://jobs.smartrecruiters.com/Acme/12345") == "SmartRecruiters"

    def test_none_for_non_ats(self):
        assert _infer_ats_name("https://sendgrid.net/wf/click?abc") is None
        assert _infer_ats_name("https://hiring.cafe/job/xyz") is None

    def test_none_for_none_input(self):
        assert _infer_ats_name(None) is None


# ── fetch_job_description integration (mocked) ────────────────────────────────

_GOOD_JD_TEXT = (
    "About the role\n\n"
    "We are hiring a Marketing Ops Lead to run our HubSpot + Salesforce stack.\n\n"
    "Responsibilities\n"
    "- Own the CRM data model and reporting cadence\n"
    "- Partner with sales leadership on pipeline hygiene\n\n"
    "Requirements\n"
    "- 5+ years in marketing ops\n"
    "- HubSpot admin experience\n\n"
    "Benefits\n- Competitive compensation and equity\n"
    * 3  # pad so len >= min_length even after cleaning
)


class TestFetchJobDescriptionSurfacesAts:
    def test_google_ats_path_surfaces_ats_url_and_name(self):
        """When _search_for_jd finds a Greenhouse URL, the result carries it."""
        gh_url = "https://boards.greenhouse.io/acme/jobs/12345"
        with patch.object(jd_fetcher, "_search_for_jd", return_value=gh_url), \
             patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
             patch.object(jd_fetcher, "_fetch_ats_page", return_value=_GOOD_JD_TEXT):
            result = fetch_job_description(
                url="https://sendgrid.net/wf/click?abc",
                timeout=5,
                min_length=200,
                job_title="Marketing Ops Lead",
                company="Acme",
            )
        assert result is not None
        assert isinstance(result, JDFetchResult)
        assert "Responsibilities" in result.text
        assert result.ats_apply_url == gh_url
        assert result.ats == "Greenhouse"

    def test_hiring_cafe_ats_fallback_surfaces_ats(self):
        """
        When google fails but hiring.cafe returns short text plus a Lever ATS link,
        _fetch_ats_page succeeds and we surface the Lever URL + name.
        """
        lever_url = "https://jobs.lever.co/acme/uuid-here"

        # google search finds nothing
        # hiring.cafe playwright fetch returns short/no-section text plus an ATS link
        # _fetch_ats_page against the Lever URL returns valid JD text
        with patch.object(jd_fetcher, "_search_for_jd", return_value=None), \
             patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
             patch.object(jd_fetcher, "_resolve_if_sendgrid", return_value="https://hiring.cafe/job/xyz"), \
             patch.object(
                 jd_fetcher,
                 "_fetch_with_playwright",
                 return_value=("too short and no section headers", lever_url),
             ), \
             patch.object(jd_fetcher, "_fetch_ats_page", return_value=_GOOD_JD_TEXT):
            result = fetch_job_description(
                url="https://sendgrid.net/wf/click?abc",
                timeout=5,
                min_length=200,
                job_title="Marketing Ops Lead",
                company="Acme",
            )
        assert result is not None
        assert isinstance(result, JDFetchResult)
        assert result.ats_apply_url == lever_url
        assert result.ats == "Lever"

    def test_pure_hiring_cafe_success_has_no_ats(self):
        """
        When hiring.cafe itself has enough content, no ATS URL is discovered.
        Result should carry ats_apply_url=None and ats=None (backward-compat null).
        """
        with patch.object(jd_fetcher, "_search_for_jd", return_value=None), \
             patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
             patch.object(jd_fetcher, "_resolve_if_sendgrid", return_value="https://hiring.cafe/job/xyz"), \
             patch.object(
                 jd_fetcher,
                 "_fetch_with_playwright",
                 return_value=(_GOOD_JD_TEXT, None),
             ):
            result = fetch_job_description(
                url="https://sendgrid.net/wf/click?abc",
                timeout=5,
                min_length=200,
                job_title="Marketing Ops Lead",
                company="Acme",
            )
        assert result is not None
        assert isinstance(result, JDFetchResult)
        assert result.text.startswith("About the role")
        assert result.ats_apply_url is None
        assert result.ats is None

    def test_hiring_cafe_ats_fallback_with_non_ats_url_drops_both(self):
        """
        _find_ats_link (jd_fetcher.py:437) can return an off-site "Apply" URL
        that is NOT on any ATS_DOMAIN (e.g. workable.com, a company careers
        page). When that URL still yields a valid JD, ats_apply_url must be
        None — a URL without a recognized vendor is unroutable for Phase 3.

        Regression for the guard added in the hiring_cafe_ats path so its
        behavior matches google_broad and pure hiring.cafe.
        """
        non_ats_apply_url = "https://acme-careers.workable.com/jobs/12345"
        with patch.object(jd_fetcher, "_search_for_jd", return_value=None), \
             patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
             patch.object(jd_fetcher, "_resolve_if_sendgrid", return_value="https://hiring.cafe/job/xyz"), \
             patch.object(
                 jd_fetcher,
                 "_fetch_with_playwright",
                 return_value=("too short and no section headers", non_ats_apply_url),
             ), \
             patch.object(jd_fetcher, "_fetch_ats_page", return_value=_GOOD_JD_TEXT):
            result = fetch_job_description(
                url="https://sendgrid.net/wf/click?abc",
                timeout=5,
                min_length=200,
                job_title="Marketing Ops Lead",
                company="Acme",
            )
        assert result is not None
        # JD text still comes through from the fallback fetch...
        assert "Responsibilities" in result.text
        # ...but ats fields are None because the URL isn't a recognized vendor.
        assert result.ats_apply_url is None
        assert result.ats is None

    def test_total_failure_returns_none(self):
        """When nothing yields valid JD, return None (unchanged failure contract)."""
        with patch.object(jd_fetcher, "_search_for_jd", return_value=None), \
             patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
             patch.object(jd_fetcher, "_resolve_if_sendgrid", return_value=None):
            result = fetch_job_description(
                url="https://sendgrid.net/wf/click?abc",
                timeout=5,
                min_length=200,
                job_title="X",
                company="Y",
            )
        assert result is None


# ── _find_ats_link unchanged-interface regression ─────────────────────────────

class TestFindAtsLinkStillWorks:
    def test_greenhouse_apply_link_discovered(self):
        """
        _find_ats_link retains its str | None signature and still identifies a
        Greenhouse ATS URL among a page's anchors.

        Post-M15 (Phase 6 audit): the internal call is now a single
        ``page.eval_on_selector_all`` returning [{href, text}, ...] rather
        than a Python-side loop over ``query_selector_all`` handles.
        """
        page = MagicMock()
        page.eval_on_selector_all.return_value = [
            {"href": "https://hiring.cafe/somewhere", "text": "Home"},
            {"href": "https://boards.greenhouse.io/acme/jobs/999", "text": "Apply Now"},
        ]

        result = _find_ats_link(page)
        assert result == "https://boards.greenhouse.io/acme/jobs/999"

    def test_no_ats_link_returns_none(self):
        page = MagicMock()
        page.eval_on_selector_all.return_value = []
        assert _find_ats_link(page) is None
