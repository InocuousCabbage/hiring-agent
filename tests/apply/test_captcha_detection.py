"""RED tests for src.apply.captcha — S9 shard (captcha-detection).

Contract per master-plan §2 (S9), §4.7, §12 #6 and spec 09-s9-captcha-detection.md:
- Pure DOM-marker detector: `detect(page: Page) -> CaptchaKind | None`.
- Ordered detection: cloudflare_turnstile -> recaptcha_v2 -> recaptcha_v3 -> hcaptcha -> datadome.
- reCAPTCHA v2 takes precedence over v3 when both markers are present.
- Never raises on missing DOM; every locator guarded by count() > 0.
- Emits ONE structlog event on positive detection: `apply.captcha_detected` with keys
  {kind, page_url} only (L7 pre-scrub — no data-sitekey, no cookies, no field values).
- Emits ZERO events when returning None.
- Runs in <=250ms wall-clock on a clean page (soft benchmark, 5 iterations).
- No goto/reload/evaluate/network/fs I/O — pure selector count() introspection.

Landmines enforced by these tests:
- L1  (no text-match): all detection uses selectors, never text=/captcha/i.
- L6  (no datetime.utcnow): asserted at file level via grep of the source module.
- L7  (log-scrub): asserted per positive-detection test (keys subset check).
- L14 (no hardcoded ATS list): S9 module never imports/mentions ATS names.
"""

from __future__ import annotations

import ast
import re
import time
import tokenize
from io import StringIO
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright
from structlog.testing import capture_logs


def _code_only(src: str) -> str:
    """Return ``src`` with all docstrings AND comments stripped.

    The landmine grep tests target actual executable code, not module- or
    function-level documentation and not comments — the latter routinely
    mention forbidden constructs in the course of explaining WHY they are
    forbidden.  This helper removes both so the greps hit only real code.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    unparsed = ast.unparse(tree)
    # ast.unparse drops `#` comments already; run tokenize to be defensive
    # against any `# type: ignore` style tokens re-emitted by future Python
    # versions.
    stripped_lines: list[str] = []
    for tok_type, tok_str, *_ in tokenize.generate_tokens(StringIO(unparsed).readline):
        if tok_type == tokenize.COMMENT:
            continue
        stripped_lines.append(tok_str)
    return "".join(stripped_lines)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures" / "apply"


@pytest.fixture(scope="module")
def browser() -> Browser:
    """One headless Chromium per test module — cheaper than per-test."""
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture()
def page(browser: Browser) -> Page:
    """Fresh page per test; context isolated so cookies don't leak.

    All outbound network requests are aborted at the route layer.  The
    CAPTCHA fixtures reference vendor domains (google.com, hcaptcha.com,
    js.datadome.co, ...) so the tags parse into the DOM and become
    discoverable by the detector's selectors, but we never want the
    browser to actually fetch them — that would (a) require internet, (b)
    make tests flake if a vendor URL 5xxs, (c) risk vendor JS mutating the
    DOM under us (e.g. Google's api.js injects an ``anchor`` iframe with
    ``size=invisible``, which used to false-positive as reCAPTCHA v2).
    """
    ctx = browser.new_context()
    ctx.route("**/*", lambda route: route.abort())
    try:
        p = ctx.new_page()
        yield p
    finally:
        ctx.close()


def _load(page: Page, filename: str) -> None:
    """Load a fixture HTML file into `page` via set_content (no network I/O)."""
    html = (FIXTURES / filename).read_text()
    page.set_content(html)


# ---------------------------------------------------------------------------
# Public-shape tests
# ---------------------------------------------------------------------------

def test_module_exports_expected_symbols():
    """Public API: CaptchaKind + detect() must be importable."""
    from src.apply import captcha

    assert hasattr(captcha, "CaptchaKind")
    assert hasattr(captcha, "detect")
    assert callable(captcha.detect)


def test_captcha_kind_is_closed_literal_with_exact_five_values():
    """CaptchaKind Literal must contain exactly the 5 kinds in spec §Interfaces."""
    import typing

    from src.apply.captcha import CaptchaKind

    args = typing.get_args(CaptchaKind)
    assert args == (
        "cloudflare_turnstile",
        "recaptcha_v2",
        "recaptcha_v3",
        "hcaptcha",
        "datadome",
    ), f"CaptchaKind args order/set drift: {args!r}"


# ---------------------------------------------------------------------------
# Positive detection — one per kind
# ---------------------------------------------------------------------------

def test_detects_cloudflare_turnstile(page: Page):
    from src.apply.captcha import detect

    _load(page, "captcha_turnstile.html")
    assert detect(page) == "cloudflare_turnstile"


def test_detects_recaptcha_v2(page: Page):
    from src.apply.captcha import detect

    _load(page, "captcha_recaptcha_v2.html")
    assert detect(page) == "recaptcha_v2"


def test_detects_recaptcha_v3_when_no_v2(page: Page):
    from src.apply.captcha import detect

    _load(page, "captcha_recaptcha_v3.html")
    assert detect(page) == "recaptcha_v3"


def test_v2_takes_precedence_over_v3_when_both_present(page: Page):
    """Precedence rule §Acceptance #4: v2 marker wins over v3 marker."""
    from src.apply.captcha import detect

    # Synthetic DOM: both v2 checkbox and v3 invisible + render script.
    page.set_content(
        """
        <html>
          <head>
            <script src="https://www.google.com/recaptcha/api.js?render=fakeV3Key"></script>
          </head>
          <body>
            <div class="g-recaptcha" data-sitekey="fakeV2Key"></div>
            <div class="g-recaptcha" data-sitekey="fakeV3Key" data-size="invisible"></div>
          </body>
        </html>
        """
    )
    assert detect(page) == "recaptcha_v2"


def test_detects_hcaptcha(page: Page):
    from src.apply.captcha import detect

    _load(page, "captcha_hcaptcha.html")
    assert detect(page) == "hcaptcha"


def test_detects_datadome(page: Page):
    from src.apply.captcha import detect

    _load(page, "captcha_datadome.html")
    assert detect(page) == "datadome"


# ---------------------------------------------------------------------------
# Negative + robustness
# ---------------------------------------------------------------------------

def test_returns_none_on_clean_page(page: Page):
    """Greenhouse-shape form with no CAPTCHA markers -> None."""
    from src.apply.captcha import detect

    _load(page, "captcha_none.html")
    assert detect(page) is None


def test_never_raises_on_empty_dom(page: Page):
    """Empty body must not raise; returns None."""
    from src.apply.captcha import detect

    page.set_content("<html><body></body></html>")
    assert detect(page) is None


# ---------------------------------------------------------------------------
# Ordering rule (spec §Acceptance #8)
# ---------------------------------------------------------------------------

def test_ordering_turnstile_beats_all_other_kinds(page: Page):
    """Synthetic DOM with all 5 marker sets — cloudflare_turnstile wins."""
    from src.apply.captcha import detect

    page.set_content(
        """
        <html>
          <head>
            <script src="https://www.google.com/recaptcha/api.js?render=v3Key"></script>
            <script src="https://js.datadome.co/tags.js"></script>
          </head>
          <body>
            <div class="cf-turnstile" data-sitekey="tKey" data-callback="cb"></div>
            <div class="g-recaptcha" data-sitekey="v2Key"></div>
            <div class="g-recaptcha" data-sitekey="v3Key" data-size="invisible"></div>
            <div class="h-captcha" data-sitekey="hKey"></div>
            <div id="ddg-captcha-wrapper">
              <div id="ddv1-captcha-container">
                <iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></iframe>
              </div>
            </div>
          </body>
        </html>
        """
    )
    assert detect(page) == "cloudflare_turnstile"


# ---------------------------------------------------------------------------
# Logging contract (spec §Acceptance #10, landmine L7)
# ---------------------------------------------------------------------------

def test_positive_detection_emits_one_log_event_with_kind_and_page_url(page: Page):
    """Exactly ONE `apply.captcha_detected` event; keys ⊂ {kind, page_url, event}.

    L7 pre-scrub: no `data-sitekey`, no `filled_fields`, no `value`, no `answer` in payload.
    """
    from src.apply.captcha import detect

    _load(page, "captcha_hcaptcha.html")
    with capture_logs() as caps:
        result = detect(page)

    assert result == "hcaptcha"
    events = [c for c in caps if c.get("event") == "apply.captcha_detected"]
    assert len(events) == 1, f"expected exactly one detection event, got {len(events)}: {caps!r}"
    ev = events[0]
    assert ev.get("kind") == "hcaptcha"
    assert isinstance(ev.get("page_url"), str) and ev.get("page_url")  # non-empty

    # L7: strip structlog-injected keys, remaining payload must be exactly {kind, page_url}.
    structlog_meta = {"event", "log_level"}
    payload_keys = set(ev.keys()) - structlog_meta
    assert payload_keys == {"kind", "page_url"}, (
        f"L7 log-scrub violation: unexpected keys in payload: {payload_keys - {'kind', 'page_url'}}"
    )


def test_no_log_event_emitted_on_clean_page(page: Page):
    """When detect() returns None, ZERO events are emitted."""
    from src.apply.captcha import detect

    _load(page, "captcha_none.html")
    with capture_logs() as caps:
        result = detect(page)

    assert result is None
    captcha_events = [c for c in caps if c.get("event", "").startswith("apply.captcha_")]
    assert captcha_events == [], f"expected 0 captcha events on None, got: {captcha_events!r}"


# ---------------------------------------------------------------------------
# Soft benchmark (spec §Acceptance #11)
# ---------------------------------------------------------------------------

def test_soft_benchmark_under_250ms_on_clean_page(page: Page):
    """5-iteration mean detect() wall-clock on a clean page must be <=250ms."""
    from src.apply.captcha import detect

    _load(page, "captcha_none.html")
    # Warm-up (first locator call primes the CDP roundtrip).
    detect(page)

    timings = []
    for _ in range(5):
        t0 = time.perf_counter()
        detect(page)
        timings.append(time.perf_counter() - t0)

    mean_ms = (sum(timings) / len(timings)) * 1000
    assert mean_ms <= 250, f"detect() mean {mean_ms:.1f}ms exceeded 250ms budget: {timings}"


# ---------------------------------------------------------------------------
# Source-level landmine grep (spec §Landmines L1, L6, L14)
# ---------------------------------------------------------------------------

_MODULE_SOURCE = Path(__file__).parent.parent.parent / "src" / "apply" / "captcha.py"


def test_no_datetime_utcnow_in_module_source():
    """L6: module must not use deprecated datetime.utcnow(). S9 has zero datetime calls."""
    code = _code_only(_MODULE_SOURCE.read_text())
    assert "datetime.utcnow" not in code, "L6 landmine: datetime.utcnow() present in captcha.py"
    assert "import datetime" not in code and "from datetime" not in code, (
        "L6 landmine: datetime module imported by captcha.py — should have zero datetime calls"
    )


def test_no_text_matcher_used_for_detection():
    """L1: detector must be selector-based only — no ``text=/.../i`` regex matcher."""
    code = _code_only(_MODULE_SOURCE.read_text())
    # Playwright text-engine forms: text=, :text(, :text-matches(. None allowed.
    assert not re.search(r'locator\(\s*[\'"]text=', code), (
        "L1 landmine: text= locator engine used in captcha.py"
    )
    assert ":text-matches(" not in code, "L1 landmine: :text-matches() used in captcha.py"
    assert ":text(" not in code, "L1 landmine: :text() pseudo used in captcha.py"


def test_no_hardcoded_ats_names_in_module():
    """L14: this shard is ATS-agnostic — no ATS name coupling in executable code."""
    code = _code_only(_MODULE_SOURCE.read_text()).lower()
    for ats in ("greenhouse", "lever", "ashby", "workday", "smartrecruiters"):
        assert ats not in code, f"L14 landmine: ATS name '{ats}' hardcoded in captcha.py code"


def test_no_forbidden_playwright_side_effects_in_module():
    """§Acceptance #9: no page.goto, page.reload, page.evaluate, network/fs I/O in detect()."""
    code = _code_only(_MODULE_SOURCE.read_text())
    for forbidden in (
        "page.goto",
        "page.reload",
        "page.evaluate",
        ".request.",
        "requests.",
        "httpx.",
        "urllib",
    ):
        assert forbidden not in code, f"forbidden side-effect '{forbidden}' present in captcha.py"
    # No filesystem I/O either — captcha.py never reads or writes files.
    assert "open(" not in code, "filesystem I/O 'open(' present in captcha.py"
    assert "pathlib" not in code.lower(), "pathlib import present in captcha.py — no fs I/O expected"


def test_module_docstring_documents_detection_order():
    """§Acceptance #8: module docstring must document detection order."""
    tree = ast.parse(_MODULE_SOURCE.read_text())
    doc = ast.get_docstring(tree) or ""
    assert doc, "captcha.py has no module docstring"
    # Ordering line must mention each kind in the correct sequence.
    for kind in ("cloudflare_turnstile", "recaptcha_v2", "recaptcha_v3", "hcaptcha", "datadome"):
        assert kind in doc, f"detection-order docstring missing kind: {kind}"
    # Sequence check: first occurrence of each kind is in _ORDER order.
    positions = [doc.index(k) for k in ("cloudflare_turnstile", "recaptcha_v2", "recaptcha_v3", "hcaptcha", "datadome")]
    assert positions == sorted(positions), (
        f"docstring lists kinds out of _ORDER sequence: positions={positions}"
    )
