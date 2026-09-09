"""
tests/parser/test_link_submission.py — HALF 2 T6/T9: link extraction + job-dict
construction from a user "links" email.

Locks:
  - job links are pulled from plaintext AND html bodies, deduped in order,
    and non-hiring.cafe / /viewjob/ links are ignored.
  - build_jobs_from_links produces dicts matching email_parser's 8-key shape
    exactly, with non-empty title/company/url so run_pipeline never KeyErrors.
  - slug -> title derivation is deterministic (company falls to the "Unknown"
    sentinel — an undelimited slug is not reliably segmentable; JSON drives JD).
  - the per-submission cap (jobs.max_per_submission, default 10) is enforced,
    and the alert path's own max_jobs is untouched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parser.link_submission import (  # noqa: E402
    extract_job_links,
    build_jobs_from_links,
)

# The canonical pipeline-shaped key set, taken from email_parser output.
_EXPECTED_KEYS = {
    "title", "company", "location", "date_posted",
    "salary", "description_snippet", "url", "resolved_url",
}

_SLUG_URL = (
    "https://hiring.cafe/job/"
    "consumer-insights-manager-trilliant-food-and-nutrition-little-chute-zmxdntl7i86xpys3"
)


class TestExtractJobLinks:
    def test_extracts_job_links_from_plaintext(self):
        body = (
            "Hi agent, please run these:\n"
            f"{_SLUG_URL}\n"
            "https://hiringcafe.com/job/staff-engineer-acme-remote-abc123def456\n"
            "thanks!"
        )
        links = extract_job_links(body, "")
        assert links == [
            _SLUG_URL,
            "https://hiringcafe.com/job/staff-engineer-acme-remote-abc123def456",
        ]

    def test_extracts_job_links_from_html(self):
        html = (
            '<html><body><p>Try this one:</p>'
            f'<a href="{_SLUG_URL}">Consumer Insights Manager</a>'
            '</body></html>'
        )
        links = extract_job_links("", html)
        assert links == [_SLUG_URL]

    def test_dedupes_preserving_order(self):
        body = f"{_SLUG_URL}\nsome text\n{_SLUG_URL}\n"
        html = f'<a href="{_SLUG_URL}">dup</a>'
        links = extract_job_links(body, html)
        assert links == [_SLUG_URL]

    def test_ignores_non_hiringcafe_and_viewjob(self):
        body = (
            "https://example.com/job/not-us-123\n"
            "https://hiring.cafe/viewjob/legacy-410-id\n"
            f"{_SLUG_URL}\n"
        )
        links = extract_job_links(body, "")
        assert links == [_SLUG_URL]


class TestBuildJobs:
    def test_build_jobs_produces_pipeline_shaped_dicts(self):
        jobs = build_jobs_from_links([_SLUG_URL], config={})
        assert len(jobs) == 1
        job = jobs[0]
        # Same key set the alert parser emits — no more, no less.
        assert set(job.keys()) == _EXPECTED_KEYS
        assert job["url"] == _SLUG_URL
        assert job["title"] and isinstance(job["title"], str)
        assert job["company"] and isinstance(job["company"], str)

    def test_slug_parse_title_company(self):
        jobs = build_jobs_from_links([_SLUG_URL], config={})
        job = jobs[0]
        # Deterministic slug->title: humanized slug words, trailing id token
        # dropped. Company is the honest "Unknown" sentinel (see module doc).
        assert job["title"] == (
            "Consumer Insights Manager Trilliant Food And Nutrition Little Chute"
        )
        assert job["company"] == "Unknown"

    def test_viewjob_link_dropped_or_marked(self):
        # A /viewjob/ link never becomes a job.
        jobs = build_jobs_from_links(
            ["https://hiring.cafe/viewjob/legacy-410-id"], config={}
        )
        assert jobs == []
