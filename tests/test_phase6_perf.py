"""
tests/test_phase6_perf.py — Behavioral tests for Phase 6 performance findings.

Each test asserts a behavioral property (call counts, cache reuse, source
shape) that would FAIL against `main` before the Phase 6 fix and PASS after.

Findings covered:
  H15 shared browser session   — assert chromium.launch invoked ≤1 across N fetches
  M16 remove fixed waits       — source-grep on unconditional 3000ms/2000ms waits
  M15 single-eval link scan    — _find_ats_link uses page.eval_on_selector_all
  M14 label cache              — get_or_create_label re-uses cached roster
  M13 Gmail batch/metadata     — search() uses batch HTTP for message fetches
  L1  auto_fix shape guard     — non-dict "resume" returns originals, no TypeError
  L5  lru_cache soffice probe  — _find_libreoffice runs subprocess once across N calls

Every test is offline (no real Chromium launch, no real Gmail).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── L5 — lru_cache on _find_libreoffice ──────────────────────────────────────


def test_l5_find_libreoffice_probes_once_across_multiple_calls(monkeypatch):
    """_find_libreoffice() must cache the probe result and reuse it across
    calls; a per-call subprocess probe blows the docx→pdf hot path."""
    from pdf_gen import renderer

    # Force cache clear — lru_cache is module-level so a prior test could
    # have primed it. Support both post-fix (cache_clear attr) and pre-fix
    # (no attr) shapes.
    if hasattr(renderer._find_libreoffice, "cache_clear"):
        renderer._find_libreoffice.cache_clear()

    calls: list[list[str]] = []

    def _fake_run(cmd, capture_output, text, timeout):
        calls.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "LibreOffice 7.0"
        result.stderr = ""
        return result

    monkeypatch.setattr(renderer.subprocess, "run", _fake_run)

    # Prime the candidates so the first-iteration succeeds.
    monkeypatch.setattr(renderer, "_LO_CANDIDATES", ["/opt/libreoffice/program/soffice"])

    try:
        for _ in range(4):
            renderer._find_libreoffice()

        assert len(calls) == 1, (
            f"expected 1 subprocess probe across 4 calls (lru_cache), "
            f"got {len(calls)}"
        )
    finally:
        # Clear the cache so the fake return value ("/opt/libreoffice/...")
        # doesn't leak into a downstream test that actually needs to detect
        # a real (or absent) LibreOffice.
        if hasattr(renderer._find_libreoffice, "cache_clear"):
            renderer._find_libreoffice.cache_clear()


# ── L1 — auto_fix shape guard ────────────────────────────────────────────────


def test_l1_auto_fix_returns_originals_on_wrong_shape(monkeypatch):
    """auto_fix must not raise TypeError when the LLM returns valid JSON with
    the wrong shape for `resume` (list instead of dict). Original inputs
    should be returned unchanged."""
    from qa import checker

    # Feed valid-JSON-wrong-shape: resume as list.
    monkeypatch.setattr(
        checker,
        "call_claude",
        lambda prompt, model=None: '{"resume": [], "cover_letter": {}}',
    )

    resume_in = {"lane": "PMM", "summary": "orig"}
    cover_in = {"body": "orig"}
    # Provide the full signature. The LLM output feeds the malformed branch,
    # so jd_text/lane/project_bank content is irrelevant — placeholders.
    result_resume, result_cover = checker.auto_fix(
        tailored_resume=resume_in,
        cover_letter=cover_in,
        issues=["something"],
        jd_text="jd",
        lane={"label": "PMM"},
        project_bank=[],
    )
    assert result_resume is resume_in


def test_l1_auto_fix_returns_originals_when_cover_letter_wrong_shape(monkeypatch):
    """Symmetric coverage: cover_letter as list should also fall back cleanly."""
    from qa import checker

    monkeypatch.setattr(
        checker,
        "call_claude",
        lambda prompt, model=None: '{"resume": {"lane": "PMM", "summary": "x"}, "cover_letter": []}',
    )
    resume_in = {"lane": "PMM", "summary": "orig"}
    cover_in = {"body": "orig"}
    result_resume, result_cover = checker.auto_fix(
        tailored_resume=resume_in,
        cover_letter=cover_in,
        issues=["x"],
        jd_text="jd",
        lane={"label": "PMM"},
        project_bank=[],
    )
    # cover_letter fell back to original — resume can be either, but MUST NOT raise.
    assert result_cover is cover_in


# ── M14 — label cache ────────────────────────────────────────────────────────


class _FakeExecutable:
    """Chainable Google-API mock: obj.execute() returns a preset payload."""

    def __init__(self, payload):
        self._payload = payload
        self.execute_calls = 0

    def execute(self):
        self.execute_calls += 1
        return self._payload


class _FakeLabelsResource:
    """Fake `service.users().labels()` supporting .list() and .create()."""

    def __init__(self):
        self.list_calls = 0
        self.create_calls = 0
        self._roster = [
            {"id": "Label_1", "name": "hiring-agent/apply/pending"},
            {"id": "Label_2", "name": "hiring-agent/apply/submitted"},
        ]

    def list(self, userId):
        self.list_calls += 1
        return _FakeExecutable({"labels": list(self._roster)})

    def create(self, userId, body):
        self.create_calls += 1
        new_id = f"Label_{len(self._roster) + 1}"
        rec = {"id": new_id, "name": body["name"]}
        self._roster.append(rec)
        return _FakeExecutable(rec)


class _FakeUsersResource:
    def __init__(self, labels_resource):
        self._labels_resource = labels_resource

    def labels(self):
        return self._labels_resource


class _FakeService:
    def __init__(self, labels_resource):
        self._users = _FakeUsersResource(labels_resource)

    def users(self):
        return self._users


def _make_gmail_client_with_fake_service(fake_service):
    """Build a GmailClient without running OAuth."""
    from gmail.client import GmailClient

    client = GmailClient.__new__(GmailClient)
    client.creds = None
    client.service = fake_service
    # M14: __new__ bypasses __init__ so the cache attr isn't set. Mirror
    # what the real constructor does so cache-aware methods work.
    client._label_cache = None
    return client


def test_m14_get_or_create_label_reuses_cached_roster():
    """Two get_or_create_label calls for an EXISTING label must issue only
    ONE labels.list() round-trip."""
    labels_resource = _FakeLabelsResource()
    service = _FakeService(labels_resource)
    client = _make_gmail_client_with_fake_service(service)

    client.get_or_create_label("hiring-agent/apply/pending")
    client.get_or_create_label("hiring-agent/apply/pending")

    assert labels_resource.list_calls == 1, (
        f"expected 1 labels.list() across 2 lookups (cache), "
        f"got {labels_resource.list_calls}"
    )


def test_m14_ensure_labels_reuses_cache_across_three_lookups():
    """ensure_labels resolves 3 labels via get_or_create_label; the cache
    must collapse those 3 lookups into a single labels.list() round-trip."""
    labels_resource = _FakeLabelsResource()
    # Seed all three so no creates fire.
    labels_resource._roster = [
        {"id": "Label_1", "name": "hiring-agent/apply/pending"},
        {"id": "Label_2", "name": "hiring-agent/apply/submitted"},
        {"id": "Label_3", "name": "hiring-agent/apply/declined"},
    ]
    service = _FakeService(labels_resource)
    client = _make_gmail_client_with_fake_service(service)

    from apply.review import ensure_labels

    config = {"apply": {"gmail_label_prefix": "hiring-agent/apply"}}
    result = ensure_labels(client, config)

    assert set(result.keys()) == {"pending", "submitted", "declined"}
    assert labels_resource.list_calls == 1, (
        f"expected 1 labels.list() across 3 ensure_labels lookups, "
        f"got {labels_resource.list_calls}"
    )


def test_m14_get_or_create_label_creates_and_caches_new_label():
    """When a label doesn't exist, get_or_create_label creates it AND caches
    the ID so a subsequent lookup issues zero additional labels.list() calls."""
    labels_resource = _FakeLabelsResource()
    service = _FakeService(labels_resource)
    client = _make_gmail_client_with_fake_service(service)

    new_id = client.get_or_create_label("hiring-agent/apply/declined")  # not seeded
    assert labels_resource.create_calls == 1
    # Second lookup MUST hit the cache — not list_labels again.
    baseline = labels_resource.list_calls
    same_id = client.get_or_create_label("hiring-agent/apply/declined")
    assert same_id == new_id
    assert labels_resource.list_calls == baseline, (
        "second lookup after create should hit cache, not refetch roster"
    )


# ── M13 — Gmail batch/metadata ───────────────────────────────────────────────


def test_m13_search_uses_batch_or_metadata_for_message_fetches():
    """search() must NOT issue one sequential messages.get(format=full) per
    result. Post-fix: a single batched HTTP round-trip regardless of result
    count.

    Round-trip = ``.execute()`` invocation. In real googleapiclient, ``.get()``
    just constructs an HttpRequest; ``.execute()`` (or ``batch.execute()``)
    fires the RPC. We track ``execute()`` calls on the message-get stubs
    and assert zero sequential fires (all fetches ride the batch)."""
    from gmail.client import GmailClient

    # Track per-message-get execute() calls — the true HTTP-round-trip signal.
    sequential_execute_calls: list[str] = []

    class _MsgGetStub:
        """Fake HttpRequest returned by messages().get(). Real client either
        calls .execute() on it (sequential — 1 RTT per stub) OR feeds it
        to batch.add() (0 RTT per stub; the batch fires them all in 1)."""

        def __init__(self, mid, payload):
            self.mid = mid
            self.payload = payload

        def execute(self):
            sequential_execute_calls.append(self.mid)
            return self.payload

    class _FakeMessagesResource:
        def __init__(self):
            self.list_calls = 0
            self.get_calls = 0
            self._refs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(10)]
            self._payloads = {
                f"m{i}": {
                    "id": f"m{i}",
                    "threadId": f"t{i}",
                    "internalDate": str(1_700_000_000 + i),
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "a@b.com"},
                            {"name": "In-Reply-To", "value": ""},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": ""},
                    },
                }
                for i in range(10)
            }

        def list(self, userId, q, maxResults):
            self.list_calls += 1
            return _FakeExecutable({"messages": list(self._refs)})

        def get(self, userId, id, format="full"):
            self.get_calls += 1
            return _MsgGetStub(id, self._payloads[id])

    class _FakeUsers2:
        def __init__(self, msgs):
            self._msgs = msgs

        def messages(self):
            return self._msgs

    class _FakeService2:
        def __init__(self, msgs):
            self._users = _FakeUsers2(msgs)
            self.batch_calls = 0
            self.batch_added: list[tuple[str, "_MsgGetStub"]] = []

        def users(self):
            return self._users

        def new_batch_http_request(self, callback=None):
            self.batch_calls += 1
            batch = MagicMock()

            def _add(request, request_id=None):
                # `request` is the _MsgGetStub returned by messages().get().
                # We record (request_id, request) but do NOT call
                # request.execute() — batch.execute() fires them all at once.
                self.batch_added.append((request_id, request))

            def _execute():
                # Fire the callback for each added request with its payload —
                # ONE HTTP round-trip in the real client.
                for rid, stub in self.batch_added:
                    if callback is not None:
                        callback(rid, stub.payload, None)

            batch.add = _add
            batch.execute = _execute
            return batch

    msgs = _FakeMessagesResource()
    service = _FakeService2(msgs)
    client = _make_gmail_client_with_fake_service(service)

    results = client.search("subject:test", max_results=10)
    assert len(results) == 10
    # POST-FIX assertion: exactly ONE new_batch_http_request AND zero
    # sequential .execute() fires on individual message stubs.
    assert service.batch_calls == 1, (
        f"expected exactly 1 batch, got {service.batch_calls}"
    )
    assert sequential_execute_calls == [], (
        f"expected zero sequential HTTP round-trips on individual gets, "
        f"got {len(sequential_execute_calls)}: {sequential_execute_calls}"
    )


# ── M15 — single-eval link scan ──────────────────────────────────────────────


def test_m15_find_ats_link_uses_single_page_evaluation():
    """_find_ats_link must extract all anchors via ONE page.eval_on_selector_all
    call rather than iterating query_selector_all + per-link get_attribute +
    per-link inner_text (2 CDP RTTs per anchor)."""
    from scraper import jd_fetcher

    page = MagicMock()

    # Post-fix: eval_on_selector_all returns a list of (href, text) tuples
    # from one JS eval; the Python code just filters.
    page.eval_on_selector_all.return_value = [
        {"href": "https://boards.greenhouse.io/testco/jobs/1", "text": "Apply Now"},
        {"href": "https://hiring.cafe/xyz", "text": "See More"},
    ]

    # In case the pre-fix path is exercised, wire query_selector_all to raise
    # so a fallback silently succeeding cannot mask a regression.
    def _boom(*args, **kwargs):
        raise AssertionError(
            "_find_ats_link must not call page.query_selector_all — "
            "use page.eval_on_selector_all for O(1) round-trips"
        )

    page.query_selector_all.side_effect = _boom

    result = jd_fetcher._find_ats_link(page)
    assert result == "https://boards.greenhouse.io/testco/jobs/1"
    page.eval_on_selector_all.assert_called_once()


# ── M16 — remove unconditional fixed waits ───────────────────────────────────


def test_m16_fetch_with_playwright_has_no_unconditional_3000ms_sleep():
    """Static assertion: the unconditional 3000 ms wait_for_timeout must be
    removed from _fetch_with_playwright.

    Rationale: the 3 s hard sleep runs BEFORE the selector-wait loop even
    when the target page is already fully rendered. Post-fix: race the
    selectors, drop the unconditional sleep. A short bounded stabilization
    check (<= 500 ms polling grace) is allowed."""
    source = (ROOT / "src" / "scraper" / "jd_fetcher.py").read_text()

    # Extract just the _fetch_with_playwright function body (from its def to
    # the next top-level def).
    import re

    m = re.search(
        r"def _fetch_with_playwright\(.*?(?=\ndef [_a-zA-Z])",
        source,
        flags=re.DOTALL,
    )
    assert m, "could not locate _fetch_with_playwright in jd_fetcher.py"
    body = m.group(0)

    # Any wait_for_timeout call ≥ 1000 ms is a fixed-wait smell. Post-fix
    # must not leave one behind in this function.
    forbidden = re.findall(r"wait_for_timeout\((\d+)\)", body)
    tall = [ms for ms in forbidden if int(ms) >= 1000]
    assert not tall, (
        f"_fetch_with_playwright still has unconditional fixed waits "
        f">= 1000ms: {tall}. Post-M16 fix should race selectors, not sleep."
    )


def test_m16_fetch_ats_page_playwright_fallback_has_no_2000ms_sleep():
    """Symmetric coverage for _fetch_ats_page's Playwright fallback (line 575)."""
    source = (ROOT / "src" / "scraper" / "jd_fetcher.py").read_text()

    import re

    m = re.search(
        r"def _fetch_ats_page\(.*?(?=\ndef [_a-zA-Z])",
        source,
        flags=re.DOTALL,
    )
    assert m, "could not locate _fetch_ats_page in jd_fetcher.py"
    body = m.group(0)

    forbidden = re.findall(r"wait_for_timeout\((\d+)\)", body)
    tall = [ms for ms in forbidden if int(ms) >= 1000]
    assert not tall, (
        f"_fetch_ats_page still has unconditional fixed waits >= 1000ms: {tall}"
    )


# ── H15 — shared browser session across fetch loop ───────────────────────────


def test_h15_shared_browser_context_manager_exists():
    """Post-fix: a public shared-browser context manager exists that opens
    Chromium ONCE and yields a reusable Browser handle."""
    from browser.session import shared_browser  # must exist post-fix

    assert callable(shared_browser)


def test_h15_fetch_job_description_accepts_shared_browser_and_launches_once():
    """When callers pass a shared Browser into fetch_job_description, the
    inner helpers must NOT call chromium.launch again."""
    from scraper import jd_fetcher

    launch_count = 0

    class _FakeBrowser:
        def new_context(self, **kwargs):
            return _FakeContext()

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            pass

    class _FakePage:
        def set_extra_http_headers(self, headers):
            pass

        def goto(self, url, wait_until=None, timeout=None):
            pass

        def wait_for_selector(self, selector, timeout=None, state=None):
            pass

        def wait_for_timeout(self, ms):
            pass

        def query_selector(self, selector):
            return None

        def query_selector_all(self, selector):
            return []

        def eval_on_selector_all(self, selector, script):
            return []

        def inner_text(self, selector):
            return (
                "Responsibilities\nBuild things\nRequirements\nSkill\n"
                * 30  # >200 chars, JD-shaped, has section headers
            )

        def content(self):
            return "<html></html>"

    shared = _FakeBrowser()

    # Any launch attempt is a regression.
    real_playwright = jd_fetcher.sync_playwright

    def _tracked_playwright(*a, **k):
        nonlocal launch_count
        launch_count += 1
        return real_playwright(*a, **k)

    with patch.object(jd_fetcher, "sync_playwright", _tracked_playwright):
        # Give hiring.cafe a resolvable URL so the Playwright path is exercised.
        with patch.object(
            jd_fetcher, "_resolve_if_sendgrid", return_value="https://hiring.cafe/x"
        ):
            with patch.object(jd_fetcher, "_search_for_jd", return_value=None):
                with patch.object(jd_fetcher, "_search_for_jd_broad", return_value=None):
                    for _ in range(3):
                        jd_fetcher.fetch_job_description(
                            url="https://hiring.cafe/x",
                            timeout=10,
                            min_length=100,
                            job_title="PMM",
                            company="Testco",
                            browser=shared,  # NEW post-H15 kwarg
                        )

    assert launch_count == 0, (
        f"expected chromium NOT to launch when shared browser is passed "
        f"across 3 fetches, got launch_count={launch_count}"
    )
