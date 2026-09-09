"""
tests/scraper/test_next_data.py — hiring.cafe #__NEXT_DATA__ JSON extraction.

The modern hiring.cafe /job page carries the JD, title, company, and apply URL
in an embedded ``<script id="__NEXT_DATA__">`` JSON blob, not in class-tagged
DOM nodes. These tests lock:

  - _parse_next_data pulls title / company / description / apply_url out of the
    captured payload shape (T5 fixture, real key path confirmed 2026-09-09).
  - company falls back to v5_processed_job_data.company_name when
    enriched_company_data is absent.
  - a garbage / wrong-shape payload returns None (never raises) so callers fall
    back to the class-selector text path.
  - _extract_next_data works end-to-end against a real rendered page (MockATSPage
    serving the saved job_page.html — offline, real Chromium).
  - a legacy /viewjob/{id} link that now returns 410 Gone is a clean skip (T4).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tests.apply._paths import (  # noqa: E402
    HIRINGCAFE_JOB_NEXT_DATA_JSON,
    HIRINGCAFE_JOB_PAGE_HTML,
)

from scraper import jd_fetcher  # noqa: E402
from scraper.jd_fetcher import (  # noqa: E402
    _extract_next_data,
    _fetch_ats_page,
    _parse_next_data,
)


def _fixture_raw() -> str:
    return HIRINGCAFE_JOB_NEXT_DATA_JSON.read_text()


class TestParseNextData:
    def test_parse_next_data_yields_all_four_fields(self):
        """The four load-bearing fields all populate from the captured payload."""
        result = _parse_next_data(_fixture_raw())
        assert result is not None
        assert result["title"] == "Consumer Insights Manager"
        assert result["company"] == "Trilliant Food & Nutrition"
        assert result["description"] and "Responsibilities" in result["description"]
        assert result["apply_url"] == (
            "https://secure2.entertimeonline.com/ta/7612.careers?ShowJob=872606803"
        )

    def test_company_falls_back_to_v5_when_enriched_absent(self):
        """company comes from v5_processed_job_data.company_name when
        enriched_company_data is missing entirely."""
        data = json.loads(_fixture_raw())
        del data["props"]["pageProps"]["job"]["enriched_company_data"]
        result = _parse_next_data(json.dumps(data))
        assert result is not None
        assert result["company"] == "Trilliant Food & Nutrition"

    def test_company_falls_back_when_enriched_name_empty(self):
        """An empty enriched_company_data.name still falls through to v5."""
        data = json.loads(_fixture_raw())
        data["props"]["pageProps"]["job"]["enriched_company_data"]["name"] = ""
        result = _parse_next_data(json.dumps(data))
        assert result is not None
        assert result["company"] == "Trilliant Food & Nutrition"

    def test_parse_next_data_returns_none_on_garbage(self):
        """Empty object, non-JSON, and valid-JSON-wrong-shape all return None
        (never raise) so the caller degrades to the text fallback."""
        assert _parse_next_data("{}") is None
        assert _parse_next_data("not json") is None
        assert _parse_next_data('{"props": {"pageProps": {"nope": 1}}}') is None
        # props present but None (a real drift shape) must not raise
        assert _parse_next_data('{"props": null}') is None
        assert _parse_next_data("") is None

    def test_extract_next_data_from_real_page(self, mock_ats_page):
        """World-assert: _extract_next_data reads the script tag from a real
        rendered page (MockATSPage serving the saved job_page.html)."""
        mock_ats_page.serve_html("**/job/**", HIRINGCAFE_JOB_PAGE_HTML)
        page = mock_ats_page.page
        page.goto("https://hiring.cafe/job/consumer-insights-manager-x", timeout=15000)
        result = _extract_next_data(page)
        assert result is not None
        assert result["title"] == "Consumer Insights Manager"
        assert result["company"] == "Trilliant Food & Nutrition"
        assert "Responsibilities" in (result["description"] or "")

    def test_extract_next_data_returns_none_when_script_absent(self):
        """No #__NEXT_DATA__ node -> None (old-page / non-hiring.cafe fallback)."""
        page = MagicMock()
        page.query_selector.return_value = None
        assert _extract_next_data(page) is None


class TestViewjob410:
    def test_viewjob_410_is_handled(self):
        """A legacy /viewjob/{id} link that now returns 410 Gone is a fast,
        legible skip: _fetch_ats_page returns None and does NOT waste a browser
        launch chasing the dead page (T4). A mock browser is supplied so the
        fallback path, if reached, would return content (proving the early
        410 return is what makes the result None)."""
        resp = MagicMock()
        resp.status_code = 410
        resp.text = "<html><body>410 Gone</body></html>"
        client_cm = MagicMock()
        client_cm.__enter__.return_value.get.return_value = resp
        client_cm.__exit__.return_value = False

        # Mock browser so the Playwright fallback (reached only if the 410 is
        # NOT short-circuited) returns real text rather than launching Chromium.
        page = MagicMock()
        page.inner_text.return_value = "This job posting is no longer available."
        browser = MagicMock()
        browser.new_context.return_value.new_page.return_value = page

        with patch.object(jd_fetcher.httpx, "Client", return_value=client_cm):
            result = _fetch_ats_page(
                "https://hiring.cafe/viewjob/abc123", timeout=5, browser=browser
            )
        assert result is None
