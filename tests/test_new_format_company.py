"""
tests/test_new_format_company.py — company extraction for the Hiring.cafe
digest format in use since ~June 2026 ("N new jobs for <search>").

The bug: in the new card markup the "Company — Location" <div> sits INSIDE a
wrapping <a>, so ``h3.find_next_sibling("div")`` skipped it and landed on the
button row, whose text is "Apply". Every job in the first live run after #18
parsed company="Apply" (50/50), which then named every output file
"Apply_<title>_..." and went into the digest.

Locks:
  - the new-format card yields the real company and location;
  - the old-format card shape still parses (the sibling <div>);
  - call-to-action text ("Apply", "View job", ...) is never accepted as a
    company, in either format; it degrades to "Unknown";
  - when the parsed company is "Unknown", run_pipeline adopts the company the
    JD fetch read from the hiring.cafe page JSON, but never a CTA label and
    never over a real parsed company;
  - the JD fetch surfaces that company on JDFetchResult.

Fixture tests/fixtures/hiringcafe/alert_new_format.html is SYNTHETIC: the
card structure of a real digest with every value replaced by a placeholder.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "hiringcafe" / "alert_new_format.html"

EXPECTED = [
    ("Marketing Coordinator", "Example Corp", "United States (Remote)"),
    ("Content Marketing Manager", "Placeholder Labs", "Boston, MA"),
    ("Growth Marketing Lead", "Sample Industries", "Portland, ME"),
    ("Lifecycle Marketing Specialist", "Fictional Co", "Austin, TX (Hybrid)"),
]


def _parse(html, max_jobs=50):
    from parser.email_parser import parse_alert_email
    return parse_alert_email(html_body=html, max_jobs=max_jobs)


# ── Parser: new format ────────────────────────────────────────────────────

def test_new_format_fixture_yields_real_companies():
    jobs = _parse(FIXTURE.read_text())
    assert [(j["title"], j["company"], j["location"]) for j in jobs] == EXPECTED


def test_new_format_no_job_has_cta_as_company():
    jobs = _parse(FIXTURE.read_text())
    assert jobs, "fixture parsed to zero jobs"
    assert all(j["company"] != "Apply" for j in jobs)


def test_new_format_other_fields_still_parse():
    first = _parse(FIXTURE.read_text())[0]
    assert first["date_posted"] == "Posted October 1, 2026"
    assert first["salary"] == "$60,000 – $65,000 / year"
    assert first["description_snippet"] == "Placeholder snippet one."
    assert first["url"] == "https://example.com/track/aaa111"


def test_new_format_card_without_salary():
    third = _parse(FIXTURE.read_text())[2]
    assert third["company"] == "Sample Industries"
    assert third["salary"] is None


# ── Parser: old format and CTA guard ─────────────────────────────────────

def _old_card(company_text):
    return f"""<table><tr><td>
<h3><span style="color:#18181a;font-weight:600;">Brand Manager</span></h3>
<div style="color:#18181a;margin-bottom:2px;font-weight:500;">{company_text}</div>
<div style="color:#75767b;font-size:.92em;">Posted today</div>
<a href="https://example.com/track/old1" style="background:#e26d8c;">Apply</a>
</td></tr></table>"""


def _new_card(company_text):
    return f"""<table><tr><td>
<h3><a href="https://example.com/track/new1">Brand Manager</a></h3>
<a href="https://example.com/track/new1" style="display:block;">
<div style="color:#18181a;margin-bottom:2px;font-weight:500;">{company_text}</div>
</a>
<div style="display:flex;"><a href="https://example.com/track/new1">Apply</a></div>
</td></tr></table>"""


def test_old_format_card_still_parses():
    (job,) = _parse(_old_card("Example Corp — Remote"))
    assert (job["company"], job["location"]) == ("Example Corp", "Remote")


def test_new_format_card_without_company_div_is_unknown_not_apply():
    """No company <div> at all: the next <div> in the card is the button row.
    That must never become the company."""
    html = """<table><tr><td>
<h3><a href="https://example.com/track/x">Brand Manager</a></h3>
<div style="display:flex;"><a href="https://example.com/track/x">Apply</a></div>
</td></tr></table>"""
    (job,) = _parse(html)
    assert job["company"] == "Unknown"


def test_company_lookup_does_not_cross_into_the_next_card():
    """A card with no <div> at all must not borrow the NEXT card's company."""
    bare = """<table><tr><td>
<h3><a href="https://example.com/track/bare">Brand Manager</a></h3>
<a href="https://example.com/track/bare">Apply</a>
</td></tr></table>"""
    jobs = _parse(bare + _new_card("Example Corp — Remote"))
    assert [j["company"] for j in jobs] == ["Unknown", "Example Corp"]


@pytest.mark.parametrize("cta", ["Apply", "apply", "Apply now", "Apply Now!", "View",
                                 "View job", "See job", "See more", "Learn more"])
@pytest.mark.parametrize("card", [_old_card, _new_card])
def test_cta_text_never_accepted_as_company(cta, card):
    (job,) = _parse(card(cta))
    assert job["company"] == "Unknown"


@pytest.mark.parametrize("name", ["Applied Materials", "Viewpoint", "Apply Digital"])
def test_real_names_that_start_like_a_cta_are_kept(name):
    (job,) = _parse(_new_card(f"{name} — Remote"))
    assert job["company"] == name


def test_two_unknown_company_cards_with_same_title_both_survive():
    """Dedup keys Unknown-company cards on URL (M7); keep that true when the
    Unknown came from the CTA guard."""
    html = (_new_card("Apply").replace("new1", "u1")
            + _new_card("Apply").replace("new1", "u2"))
    jobs = _parse(html)
    assert [j["url"] for j in jobs] == ["https://example.com/track/u1",
                                        "https://example.com/track/u2"]


# ── JD fetch surfaces the page company ───────────────────────────────────

_JD = (
    "About the role\n\nPlaceholder JD body.\n\n"
    "Responsibilities\n- Own the thing\n\n"
    "Requirements\n- 3+ years\n\n"
    "Benefits\n- Placeholder benefits\n"
    * 4
)


def test_fetch_result_carries_company_from_next_data():
    from scraper import jd_fetcher
    page = MagicMock(name="page")
    browser = MagicMock(name="browser")
    browser.new_context.return_value.new_page.return_value = page
    next_data = {"title": "Brand Manager", "company": "Example Corp",
                 "description": _JD, "apply_url": None}
    with patch.object(jd_fetcher, "_search_for_jd", return_value=None), \
         patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None), \
         patch.object(jd_fetcher, "_resolve_if_sendgrid",
                      return_value="https://hiring.cafe/job/brand-manager-abc123"), \
         patch.object(jd_fetcher, "_extract_next_data", return_value=next_data), \
         patch.object(jd_fetcher, "_extract_best_text", return_value=""), \
         patch.object(jd_fetcher, "_find_ats_link", return_value=None):
        result = jd_fetcher.fetch_job_description(
            url="https://hiring.cafe/job/brand-manager-abc123",
            timeout=5, min_length=200, job_title="", company="", browser=browser,
        )
    assert result is not None
    assert result.company == "Example Corp"


# ── run_pipeline: Unknown falls back to the JD company ───────────────────

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    import src.main as main_mod

    state = SimpleNamespace(jd=None, rendered=[])

    monkeypatch.setattr(main_mod, "fetch_job_description", lambda **kw: state.jd)
    monkeypatch.setattr(main_mod, "classify_lane", lambda **kw: {"name": "pmm", "label": "PMM"})
    monkeypatch.setattr(main_mod, "tailor_resume", lambda **kw: {"confidence_score": 100, "summary": "x"})
    monkeypatch.setattr(main_mod, "write_cover_letter", lambda **kw: {"body": "x"})
    monkeypatch.setattr(main_mod, "run_qa", lambda **kw: {"pass": True, "errors": []})
    monkeypatch.setattr(main_mod, "auto_fix", lambda **kw: (kw["tailored_resume"], kw["cover_letter"]))

    def _render_resume(**kw):
        state.rendered.append(kw["job"]["company"])
        return tmp_path / "r.pdf", tmp_path / "r.docx"

    monkeypatch.setattr(main_mod, "render_resume", _render_resume)
    monkeypatch.setattr(main_mod, "render_cover_letter", lambda **kw: (tmp_path / "c.pdf", tmp_path / "c.docx"))
    monkeypatch.setattr(main_mod, "find_hiring_manager", lambda **kw: None)
    monkeypatch.setattr(main_mod, "_validate_apply_config", lambda cfg: None)

    def run(parsed_company, jd_company):
        state.jd = SimpleNamespace(text="JD", ats=None, ats_apply_url=None, company=jd_company)
        processed, _skipped, _events = main_mod.run_pipeline(
            jobs=[{"title": "Brand Manager", "company": parsed_company,
                   "url": "https://example.com/track/p1"}],
            config={
                "apply": {"enabled": False},
                "scraper": {"timeout_seconds": 5, "min_jd_length": 10},
                "lanes": [], "resume": {"min_confidence_score": 30},
                "cover_letter": {}, "qa": {"max_retries": 0},
            },
            project_bank=[], today="2026-10-05", output_dir=tmp_path,
        )
        return processed, state.rendered

    return run


def test_unknown_company_adopts_jd_company(pipeline):
    processed, rendered = pipeline("Unknown", "Example Corp")
    assert processed[0]["company"] == "Example Corp"
    assert rendered == ["Example Corp"]  # renderer (file names) sees it too


def test_parsed_company_is_not_overridden_by_jd(pipeline):
    processed, _ = pipeline("Placeholder Labs", "Example Corp")
    assert processed[0]["company"] == "Placeholder Labs"


@pytest.mark.parametrize("jd_company", [None, "", "Apply", "  "])
def test_unusable_jd_company_leaves_unknown(pipeline, jd_company):
    processed, _ = pipeline("Unknown", jd_company)
    assert processed[0]["company"] == "Unknown"


def test_jd_result_without_company_attr_is_tolerated(pipeline, monkeypatch, tmp_path):
    """Older JD result shapes (no .company attribute) must not crash."""
    import src.main as main_mod
    pipeline("Unknown", None)  # installs the stage stubs
    monkeypatch.setattr(main_mod, "fetch_job_description",
                        lambda **kw: SimpleNamespace(text="JD", ats=None, ats_apply_url=None))
    processed, _s, _e = main_mod.run_pipeline(
        jobs=[{"title": "Brand Manager", "company": "Unknown",
               "url": "https://example.com/track/p2"}],
        config={
            "apply": {"enabled": False},
            "scraper": {"timeout_seconds": 5, "min_jd_length": 10},
            "lanes": [], "resume": {"min_confidence_score": 30},
            "cover_letter": {}, "qa": {"max_retries": 0},
        },
        project_bank=[], today="2026-10-05", output_dir=tmp_path,
    )
    assert processed[0]["company"] == "Unknown"
