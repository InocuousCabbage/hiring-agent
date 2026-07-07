"""Greenhouse ATS adapter (S8) — the load-bearing MVP shard.

Two-layer split (per variation-A winner):
    * `plan_form_fill(html, profile, boards_api_schema=None) -> list[FieldFill]`
        Pure function — no Playwright, no I/O, no `datetime.now`. Given the
        same inputs produces byte-identical output.

    * `GreenhouseAdapter.apply(page, ctx) -> ApplyResult`
        Playwright driver — takes an already-open `Page` (transport-agnostic
        per variation-D; S10's BrowserbaseTransport hands the same Page in).

Landmine avoidance (verbatim from master-plan §10 + judge-output tiebreaker):
    * L1: confirmation requires BOTH a specific DOM marker AND a URL delta.
      Text-only match is never trusted.
    * L2/L11: every `answers_attributes` field is resolved via
      `_labels.resolve(html, question_text)`; the planner NEVER emits a
      first-match positional selector for the answers-attributes group.
    * L3: submit click is scoped to
      `form#application_form button[type='submit']:has-text('Submit Application')`.
    * L4: every `select_option` call uses `label=` kwarg — never positional.
    * L5: browser lifecycle is fully wrapped in try/finally.
    * L6: `datetime.now(timezone.utc)` everywhere; never the naive-UTC helper.
    * L7: no field VALUES touch a log record (labels + selectors + presence only).
    * L10: every `page.locator(sel).count()` runs before `page.fill` /
      `page.check` / `page.set_input_files`. Required-field miss returns
      `review_required`; optional miss is skipped with a debug log.

Cross-shard contracts consumed:
    * S1 CandidateProfile (duck-typed at runtime).
    * S2 ApplyResult / ApplyContext (from `src.apply.types`).
    * S5 DedupDB methods: `was_applied`, `soft_warn_check`, `count_today`,
      `record` — all called via `ctx.dedup`. Signatures:
        was_applied(company, ats_domain, ats_job_id, job_url) -> bool
        soft_warn_check(company_norm, role_norm) -> list[dict]
        count_today(ats_domain) -> int
        record(result, applicant, company, role_title, job_url) -> None
    * S9 captcha detector: `ctx.captcha_detector(page) -> str | None`.
      String is the CAPTCHA kind (`cloudflare_turnstile`, `recaptcha_v2`, ...).

Best-effort behaviors:
    * `application_id` extraction from the confirmation DOM: reads
      `[data-qa='application-id']` first, then the confirmation container
      text via a small regex. Missing ID is not a failure — the adapter
      returns `submitted` with `application_id=None` (spec ambiguity #3).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.apply.adapters._labels import (
    FieldFill,
    LabelledField,
    enumerate_questions,
    resolve,
)
from src.apply.dedup import (
    AlreadyAppliedError,
    _extract_ats_domain,
    _extract_ats_job_id,
)

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

    from src.apply.profile import CandidateProfile
    from src.apply.types import ApplyContext, ApplyResult
else:  # runtime — ApplyResult must be importable to be constructed
    from src.apply.types import ApplyResult


# ── Logger (S16's PII scrubber attaches processors at package boot) ──────────
log = logging.getLogger("apply.greenhouse")


# ── Constants ────────────────────────────────────────────────────────────────

_ADAPTER_NAME = "greenhouse"
_ADAPTER_DOMAINS = ("boards.greenhouse.io", "job-boards.greenhouse.io")

_BOARDS_API_TEMPLATE = (
    "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"
)
_BOARDS_API_TIMEOUT_SECONDS = 5.0

# L3: scoped submit locator.
_SUBMIT_LOCATOR = (
    "form#application_form button[type='submit']:has-text('Submit Application')"
)

# L1: DOM confirmation markers. Any of these visible + URL delta = success.
_CONFIRMATION_LOCATOR = (
    "[class*='application-confirmation'], "
    "[data-qa='confirmation'], "
    "[data-confirmation]"
)

_CONFIRMATION_WAIT_TIMEOUT_MS = 15_000

# Best-effort application ID extractors (spec ambiguity #3).
_APP_ID_QA_SELECTOR = "[data-qa='application-id']"
_APP_ID_TEXT_RE = re.compile(r"(?:reference|application)\s*(?:id|#|number)\s*[:.\-]?\s*([A-Za-z0-9\-_]+)", re.IGNORECASE)


# ── Question mapping (label -> profile getter) ───────────────────────────────


def _profile_first(p: Any) -> Any:
    return getattr(getattr(p, "name", None), "first", None)


def _profile_last(p: Any) -> Any:
    return getattr(getattr(p, "name", None), "last", None)


def _profile_full(p: Any) -> Any:
    return getattr(getattr(p, "name", None), "full", None)


def _profile_email(p: Any) -> Any:
    return getattr(getattr(p, "contact", None), "email", None)


def _profile_phone(p: Any) -> Any:
    return getattr(getattr(p, "contact", None), "phone", None)


def _profile_linkedin(p: Any) -> Any:
    return getattr(getattr(p, "contact", None), "linkedin_url", None)


def _profile_portfolio(p: Any) -> Any:
    return getattr(getattr(p, "contact", None), "portfolio_url", None)


def _profile_github(p: Any) -> Any:
    return getattr(getattr(p, "contact", None), "github_url", None)


def _profile_work_auth_yesno(p: Any) -> Any:
    v = getattr(getattr(p, "work_authorization", None), "us_authorized", None)
    if v is None:
        return None
    return "Yes" if v else "No"


def _profile_needs_sponsorship_yesno(p: Any) -> Any:
    v = getattr(getattr(p, "work_authorization", None), "requires_sponsorship", None)
    if v is None:
        return None
    return "Yes" if v else "No"


# Ordered by specificity so the FIRST match wins in cases where a label
# contains keywords for multiple entries (e.g. "First Name*" checks "first name"
# before "name"). The single-word "name" mapping is intentionally last.
_QUESTION_MAP: tuple[tuple[str, Any, Literal["fill", "select_option_by_label", "check", "upload"]], ...] = (
    ("first name", _profile_first, "fill"),
    ("last name", _profile_last, "fill"),
    ("full name", _profile_full, "fill"),
    ("email", _profile_email, "fill"),
    ("phone", _profile_phone, "fill"),
    ("linkedin", _profile_linkedin, "fill"),
    ("portfolio", _profile_portfolio, "fill"),
    ("github", _profile_github, "fill"),
    ("authorized to work", _profile_work_auth_yesno, "select_option_by_label"),
    ("work authorization", _profile_work_auth_yesno, "select_option_by_label"),
    ("visa sponsorship", _profile_needs_sponsorship_yesno, "select_option_by_label"),
    ("require sponsorship", _profile_needs_sponsorship_yesno, "select_option_by_label"),
)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _match_question(label: str) -> tuple[Any, str] | None:
    """Return (profile-getter, strategy) for the first mapping keyword whose
    tokens all appear (as whole words) in the normalized label; None on no
    match.

    Whole-word match prevents false positives like "have you emailed us
    before?" hitting the `email` mapping.
    """
    tokens = set(_WORD_RE.findall((label or "").lower()))
    if not tokens:
        return None
    for keyword, getter, strategy in _QUESTION_MAP:
        key_tokens = set(_WORD_RE.findall(keyword))
        if key_tokens and key_tokens.issubset(tokens):
            return getter, strategy
    return None


def _strategy_for_input_type(input_type: str, mapping_strategy: str) -> str:
    """Reconcile the mapping's strategy with the actual DOM input type.

    A profile like `us_authorized: True` maps to `"select_option_by_label"`,
    but if the DOM renders that question as a text input we'd fall back to
    `"fill"`. Files always upload, selects always use label-lookup.
    """
    if input_type == "select":
        return "select_option_by_label"
    if input_type == "file":
        return "upload"
    if input_type == "checkbox":
        return "check"
    return mapping_strategy if mapping_strategy != "select_option_by_label" else "fill"


# ── Boards API preflight ─────────────────────────────────────────────────────


_BOARDS_URL_RE = re.compile(
    r"^https?://(?:boards|job-boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)",
    re.IGNORECASE,
)


def _extract_board_token_and_job_id(url: str) -> tuple[str | None, str | None]:
    """Parse a Greenhouse job URL into (board_token, job_id).

    Returns (None, None) on any parse failure. Never raises.
    """
    if not url:
        return None, None
    m = _BOARDS_URL_RE.match(url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _fetch_boards_api(url: str) -> dict | None:
    """Best-effort fetch of the Greenhouse Boards API schema for `url`.

    Returns the parsed JSON dict on 200, `None` on ANY failure (404,
    timeout, JSON parse error, network error). Never raises — the driver
    must be able to fall back to DOM introspection cleanly.
    """
    token, job_id = _extract_board_token_and_job_id(url)
    if not token or not job_id:
        return None

    api_url = _BOARDS_API_TEMPLATE.format(token=token, job_id=job_id)
    try:
        resp = httpx.get(api_url, timeout=_BOARDS_API_TIMEOUT_SECONDS)
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    except Exception:  # network / DNS / SSL surface — never propagate
        return None

    if getattr(resp, "status_code", None) != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, TypeError):
        return None

    return data if isinstance(data, dict) else None


# ── Planner (pure function) ──────────────────────────────────────────────────


def _plan_from_labelled_field(
    lf: LabelledField,
    profile: Any,
    source: Literal["boards_api", "label_scan", "fallback"],
) -> FieldFill | None:
    """Turn one LabelledField into a FieldFill, or None if we skip it."""
    match = _match_question(lf.label)
    if match is None:
        return None
    getter, mapping_strategy = match
    value = getter(profile)
    if value is None or value == "":
        return None
    strategy = _strategy_for_input_type(lf.input_type, mapping_strategy)
    return FieldFill(
        selector=lf.selector,
        strategy=strategy,  # type: ignore[arg-type]
        value=value,
        label=lf.label,
        required=lf.required,
        source=source,
    )


def _boards_api_fields(schema: dict) -> Iterable[LabelledField]:
    """Adapt a Greenhouse Boards API `questions` array into LabelledFields.

    Boards API fields carry a `name` attribute that maps 1:1 to the DOM's
    input `name`. We emit `[name='...']` selectors so the driver can locate
    the DOM input without relying on the (renumberable) `#job_application_*`
    ID scheme (L11).
    """
    questions = schema.get("questions") or []
    for q in questions:
        if not isinstance(q, dict):
            continue
        label = q.get("label") or ""
        required = bool(q.get("required", False))
        fields = q.get("fields") or []
        for f in fields:
            if not isinstance(f, dict):
                continue
            name_attr = f.get("name") or ""
            if not name_attr:
                continue
            input_type = _map_boards_api_type(f.get("type") or "input_text")
            yield LabelledField(
                label=label,
                selector=f"[name='{name_attr}']",
                input_type=input_type,  # type: ignore[arg-type]
                required=required,
                name_attr=name_attr,
            )


_BOARDS_API_TYPE_MAP = {
    "input_text": "text",
    "input_file": "file",
    "textarea": "textarea",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "select",
    "single_checkbox": "checkbox",
}


def _map_boards_api_type(t: str) -> str:
    return _BOARDS_API_TYPE_MAP.get(t, "text")


#: Sentinel value the driver substitutes with the real `ctx.resume_path`.
#: The planner is pure and never touches ctx — but we still surface the
#: resume upload slot so its `required=True` propagates through the plan.
RESUME_SENTINEL = Path("<profile.resume_path>")


def plan_form_fill(
    html: str,
    profile: Any,
    *,
    boards_api_schema: dict | None = None,
) -> list[FieldFill]:
    """Return a list of `FieldFill` describing exactly what the driver will do.

    Pure: no I/O, no `datetime.now`, no Playwright. Given the same inputs
    produces byte-identical output.

    Preference order:
        * boards_api_schema present  -> source="boards_api"
        * else DOM enumerate         -> source="label_scan"

    Fields whose profile value is None are omitted (spec §Acceptance-criterion
    12 rule for optional fields). Required-but-absent fields are the driver's
    concern via presence-check (L10), not the planner's.

    Resume/CV file inputs are ALWAYS emitted (with a sentinel `Path` value)
    so their `required=True` flag surfaces through the plan; the driver
    substitutes the actual resume path from `ctx.resume_path` at execute time.
    """
    fields: list[LabelledField]
    source: Literal["boards_api", "label_scan"]
    if boards_api_schema:
        fields = list(_boards_api_fields(boards_api_schema))
        source = "boards_api"
    else:
        fields = enumerate_questions(html)
        source = "label_scan"

    out: list[FieldFill] = []
    seen_selectors: set[str] = set()
    for lf in fields:
        # File inputs are handled as resume-uploads if the label looks like one.
        if lf.input_type == "file" and _label_looks_like_resume(lf.label):
            fill = FieldFill(
                selector=lf.selector,
                strategy="upload",
                value=RESUME_SENTINEL,
                label=lf.label,
                required=lf.required,
                source=source,
            )
        else:
            fill = _plan_from_labelled_field(lf, profile, source)
        if fill is None:
            continue
        if fill.selector in seen_selectors:
            continue
        seen_selectors.add(fill.selector)
        out.append(fill)
    return out


def _label_looks_like_resume(label: str) -> bool:
    norm = (label or "").lower()
    return "resume" in norm or "cv" in norm


# ── Confirmation verification (L1) ───────────────────────────────────────────


def _verify_confirmation(
    page: "Page", pre_submit_url: str
) -> tuple[bool, str | None]:
    """L1: DOM marker present AND URL delta.

    Returns (True, application_id_or_None) on success, (False, None) on any
    failure (marker missing, URL unchanged, or wait_for timeout).
    """
    try:
        page.locator(_CONFIRMATION_LOCATOR).first.wait_for(
            state="visible", timeout=_CONFIRMATION_WAIT_TIMEOUT_MS
        )
    except Exception:
        return False, None

    post_url = getattr(page, "url", "")
    if not post_url or post_url == pre_submit_url:
        return False, None

    return True, _extract_application_id(page)


def _extract_application_id(page: "Page") -> str | None:
    """Best-effort application-ID scrape from the confirmation DOM.

    Never a hard failure — spec ambiguity #3.
    """
    try:
        html = page.content()
    except Exception:
        return None
    if not html:
        return None

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    node = soup.select_one(_APP_ID_QA_SELECTOR)
    if node is not None:
        text = node.get_text(strip=True)
        if text:
            return text

    # Fall back to scanning the confirmation container text.
    container = soup.select_one(_CONFIRMATION_LOCATOR)
    if container is not None:
        text = container.get_text(" ", strip=True)
        m = _APP_ID_TEXT_RE.search(text or "")
        if m:
            return m.group(1)
    return None


# ── Timestamps (L6) ──────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    """UTC now as ISO-8601 with `+00:00` suffix. Timezone-aware — L6 compliant."""
    return datetime.now(timezone.utc).isoformat()


# ── Screenshot / trace paths ────────────────────────────────────────────────


def _screenshot_path(ctx: Any, suffix: str) -> Path:
    base = ctx.config.get("screenshot_dir") or "state/screenshots"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return p / f"greenhouse_{stamp}_{suffix}.png"


def _trace_path(ctx: Any, suffix: str) -> Path:
    base = ctx.config.get("trace_dir") or "state/traces"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return p / f"greenhouse_{stamp}_{suffix}.zip"


def _take_screenshot(page: "Page", path: Path) -> Path | None:
    """Best-effort screenshot; failure returns None rather than raising."""
    try:
        page.screenshot(path=str(path))
    except Exception:
        return None
    return path if path.exists() else None


def _write_trace_placeholder(path: Path) -> Path:
    """Stub trace file. S11's retry decorator writes the real zip; on tests the
    file just needs to exist for verification.
    """
    try:
        path.write_bytes(b"")
    except Exception:  # pragma: no cover
        return path
    return path


# ── Field-fill execution (L4, L10) ───────────────────────────────────────────


def _execute_fill(page: "Page", fill: FieldFill) -> tuple[bool, str | None]:
    """Run one FieldFill against `page`.

    Returns (present, reason_if_missing).
        present=True on success.
        present=False, reason=str on required-field miss.
        present=False, reason=None on optional-field miss (caller skips).

    L4: `select_option_by_label` -> `page.select_option(sel, label=value)`.
    L10: locator.count() BEFORE fill; missing required field -> caller
         returns `review_required` with descriptive reason.
    """
    try:
        count = page.locator(fill.selector).count()
    except Exception:
        count = 0

    if count == 0:
        if fill.required:
            log.info(
                "apply.field.absent",
                extra={
                    "selector": fill.selector,
                    "label": fill.label,
                    "required": True,
                },
            )
            return False, f"required field missing: {fill.label}"
        # Optional-miss is a debug event per spec §12 — never a warning.
        log.debug(
            "apply.field.absent",
            extra={
                "selector": fill.selector,
                "label": fill.label,
                "required": False,
            },
        )
        return False, None

    strategy = fill.strategy
    if strategy == "select_option_by_label":
        # L4: label= kwarg, never positional value.
        page.select_option(fill.selector, label=str(fill.value))
    elif strategy == "check":
        page.check(fill.selector)
    elif strategy == "upload":
        # `value` is a Path or Path-like.
        page.set_input_files(fill.selector, str(fill.value))
    else:  # "fill"
        page.fill(fill.selector, str(fill.value))
    return True, None


# ── Result helpers ───────────────────────────────────────────────────────────


def _apply_result_success(
    *,
    apply_url: str,
    application_id: str | None,
    screenshot: Path | None,
    submitted_at: str,
    trace_path: Path | None,
) -> ApplyResult:
    return ApplyResult(
        status="submitted",
        ats=_ADAPTER_NAME,
        apply_url=apply_url,
        application_id=application_id,
        confirmation_screenshot=screenshot,
        submitted_at=submitted_at,
        trace_path=trace_path,
    )


def _apply_result_failure(
    *,
    apply_url: str | None,
    reason: str,
    screenshot: Path | None,
    trace_path: Path | None,
) -> ApplyResult:
    return ApplyResult(
        status="failed",
        ats=_ADAPTER_NAME,
        apply_url=apply_url,
        reason=reason,
        confirmation_screenshot=screenshot,
        trace_path=trace_path,
    )


# ── Adapter class ────────────────────────────────────────────────────────────


class GreenhouseAdapter:
    """Deterministic Greenhouse ATSAdapter (S8).

    Implements the S2 `ATSAdapter` Protocol: `name`, `domains`, `detect(url)`,
    `apply(page, ctx)`. Adds the optional variation-A layer:
    `plan_form_fill(html, profile)`.
    """

    name: str = _ADAPTER_NAME
    domains: tuple[str, ...] = _ADAPTER_DOMAINS

    # -- Detection ------------------------------------------------------------

    def detect(self, url: str) -> bool:
        """True iff `urlparse(url).hostname` ends with any adapter domain.

        Case-insensitive; never raises.
        """
        if not url or not isinstance(url, str):
            return False
        try:
            host = urlparse(url).hostname
        except Exception:
            return False
        if not host:
            return False
        host = host.lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

    # -- Planner (variation-A pure layer) -------------------------------------

    def plan_form_fill(
        self,
        html: str,
        profile: Any,
        *,
        boards_api_schema: dict | None = None,
    ) -> list[FieldFill]:
        """See module-level `plan_form_fill` for full contract."""
        return plan_form_fill(html, profile, boards_api_schema=boards_api_schema)

    # -- Driver ---------------------------------------------------------------

    def apply(self, page: "Page", ctx: Any) -> ApplyResult:  # noqa: C901 — single-concern flow
        """Execute the full apply cycle for one Greenhouse posting.

        Contract: never raises. Every abnormal exit path returns an
        `ApplyResult` with a descriptive `status` + `reason`.
        """
        job = ctx.job or {}
        apply_url = job.get("apply_url") or job.get("url") or ""
        company = job.get("company") or ""
        role_title = job.get("role") or job.get("title") or ""
        applicant = getattr(ctx, "applicant", "") or ""

        mode: str = getattr(ctx, "mode", ctx.config.get("mode", "review"))
        dry_run: bool = bool(getattr(ctx, "dry_run", ctx.config.get("dry_run", False)))

        # ── Gate 1: HARD dedup (before any browser touch) ──
        # H5 fix: was_applied must query with the SAME (ats_domain, ats_job_id)
        # shape DedupDB.record writes with — i.e. _extract_ats_domain(apply_url)
        # ('boards.greenhouse.io'), NOT self.name ('greenhouse'). Otherwise the
        # gate misses and the follow-up record() at end-of-flow raises
        # AlreadyAppliedError, previously swallowed silently → double-apply.
        try:
            hit = ctx.dedup.was_applied(
                company,
                _extract_ats_domain(apply_url),
                _extract_ats_job_id(apply_url),
                apply_url,
            )
        except Exception:
            hit = False
        if hit:
            log.info("apply.dedup_hit", extra={"ats": self.name})
            return ApplyResult(
                status="already_applied",
                ats=self.name,
                apply_url=apply_url,
            )

        # ── Gate 2: rate limit ──
        try:
            today_count = ctx.dedup.count_today(self.name)
        except Exception:
            today_count = 0
        cap = int(ctx.config.get("rate_limit_per_ats_per_day", 10) or 10)
        if today_count >= cap:
            log.info(
                "apply.rate_limited",
                extra={"ats": self.name, "count_today": today_count, "cap": cap},
            )
            return ApplyResult(
                status="skipped",
                ats=self.name,
                apply_url=apply_url,
                reason="rate_limited",
            )

        # ── Gate 3: soft-dup warn (does NOT short-circuit; just tags status) ──
        try:
            soft_warn = ctx.dedup.soft_warn_check(
                (company or "").strip().lower(),
                (role_title or "").strip().lower(),
            ) or []
        except Exception:
            soft_warn = []
        soft_warn_active = bool(soft_warn)

        # ── Browser lifecycle — L5 try/finally ──
        pre_submit_url: str = ""
        screenshot: Path | None = None
        trace: Path | None = None
        try:
            # Navigate. `page` is already open (transport-agnostic per spec).
            page.goto(apply_url)
            log.info("apply.form_navigated", extra={"ats": self.name})
            pre_submit_url = getattr(page, "url", apply_url) or apply_url

            # Boards API preflight (best-effort — never blocks on failure).
            schema = _fetch_boards_api(apply_url)
            if schema:
                log.info(
                    "apply.preflight.boards_api", extra={"ats": self.name, "hit": True}
                )
            else:
                log.info(
                    "apply.preflight.boards_api", extra={"ats": self.name, "hit": False}
                )

            # Plan the fill (pure).
            try:
                html = page.content()
            except Exception:
                html = ""
            plan = plan_form_fill(html, ctx.profile, boards_api_schema=schema)

            # Substitute the resume sentinel with ctx.resume_path (PDF) or
            # ctx.resume_docx_path (DOCX fallback for the dual-output renderer's
            # docx-only lane — see .agent/one-big-feature/auto-apply-2026-07-06/
            # 05-renderer-contract-audit.md). If BOTH are None, refuse to
            # substitute — passing the RESUME_SENTINEL literal to
            # page.set_input_files() would crash with "no such file".
            resume_path = getattr(ctx, "resume_path", None)
            resume_docx_path = getattr(ctx, "resume_docx_path", None)
            upload_path = resume_path or resume_docx_path
            if not upload_path and any(f.value is RESUME_SENTINEL for f in plan):
                log.warning(
                    "apply.greenhouse.no_resume_available",
                    extra={"ats": self.name, "reason": "no_resume_available"},
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "failed"))
                return ApplyResult(
                    status="failed",
                    ats=self.name,
                    apply_url=ctx.job.get("apply_url") or ctx.job.get("url"),
                    reason="no_resume_available",
                    trace_path=trace,
                )
            plan = [
                (
                    FieldFill(
                        selector=f.selector,
                        strategy=f.strategy,
                        value=Path(upload_path) if upload_path else f.value,
                        label=f.label,
                        required=f.required,
                        source=f.source,
                    )
                    if f.value is RESUME_SENTINEL
                    else f
                )
                for f in plan
            ]

            log.info(
                "apply.form_filled.start",
                extra={"ats": self.name, "fills": len(plan)},
            )
            for fill in plan:
                present, reason = _execute_fill(page, fill)
                if not present and reason is not None:
                    # Required field missing -> stop and route to review.
                    screenshot = _take_screenshot(
                        page, _screenshot_path(ctx, "review_required")
                    )
                    trace = _write_trace_placeholder(_trace_path(ctx, "review_required"))
                    log.info(
                        "apply.review_required",
                        extra={"ats": self.name, "reason": reason},
                    )
                    return ApplyResult(
                        status="review_required",
                        ats=self.name,
                        apply_url=apply_url,
                        reason=reason,
                        confirmation_screenshot=screenshot,
                        trace_path=trace,
                    )
            log.info("apply.form_filled", extra={"ats": self.name})

            # ── Gate 4: CAPTCHA (after fill, before submit) ──
            detector = getattr(ctx, "captcha_detector", None)
            captcha_kind = None
            if callable(detector):
                try:
                    captcha_kind = detector(page)
                except Exception:
                    captcha_kind = None
            if captcha_kind:
                log.info(
                    "apply.captcha_detected",
                    extra={"ats": self.name, "kind": str(captcha_kind)},
                )
                screenshot = _take_screenshot(
                    page, _screenshot_path(ctx, "captcha")
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "captcha"))
                return ApplyResult(
                    status="captcha_escalated",
                    ats=self.name,
                    apply_url=apply_url,
                    reason=f"captcha: {captcha_kind}",
                    confirmation_screenshot=screenshot,
                    trace_path=trace,
                )

            # ── Mode branches ──

            # Spec §13c: soft-warn NEVER auto-submits. Regardless of
            # `apply.mode`, a non-empty `soft_warn_check` result forces the
            # driver into a review-mode return with status="soft_dup_warn".
            if mode == "review" or soft_warn_active:
                screenshot = _take_screenshot(
                    page, _screenshot_path(ctx, "review_required")
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "review_required"))
                status_override = "soft_dup_warn" if soft_warn_active else "review_required"
                if status_override == "soft_dup_warn":
                    log.info(
                        "apply.review_required.soft_dup_warn",
                        extra={"ats": self.name, "prior_count": len(soft_warn)},
                    )
                else:
                    log.info(
                        "apply.review_required", extra={"ats": self.name}
                    )
                return ApplyResult(
                    status=status_override,
                    ats=self.name,
                    apply_url=apply_url,
                    confirmation_screenshot=screenshot,
                    trace_path=trace,
                )

            # Auto mode + dry-run: hold at pre-submit; never click.
            if dry_run:
                screenshot = _take_screenshot(
                    page, _screenshot_path(ctx, "dry_run_holding")
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "dry_run"))
                log.info(
                    "apply.dry_run.holding_at_submit", extra={"ats": self.name}
                )
                return ApplyResult(
                    status="review_required",
                    ats=self.name,
                    apply_url=apply_url,
                    reason="dry_run",
                    confirmation_screenshot=screenshot,
                    trace_path=trace,
                )

            # Auto mode: scoped submit (L3), confirmation verify (L1).
            page.locator(_SUBMIT_LOCATOR).click()
            confirmed, application_id = _verify_confirmation(page, pre_submit_url)
            if not confirmed:
                screenshot = _take_screenshot(
                    page, _screenshot_path(ctx, "failed")
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "failed"))
                log.info(
                    "apply.failed",
                    extra={
                        "ats": self.name,
                        "reason": "confirmation marker not found",
                    },
                )
                return _apply_result_failure(
                    apply_url=apply_url,
                    reason="confirmation marker not found",
                    screenshot=screenshot,
                    trace_path=trace,
                )

            screenshot = _take_screenshot(
                page, _screenshot_path(ctx, "submitted")
            )
            trace = _write_trace_placeholder(_trace_path(ctx, "submitted"))
            submitted_at = _utcnow_iso()
            result = _apply_result_success(
                apply_url=apply_url,
                application_id=application_id,
                screenshot=screenshot,
                submitted_at=submitted_at,
                trace_path=trace,
            )
            # Only record to dedup on a DOM-verified submission.
            # H5 fix: narrow the except to AlreadyAppliedError so real
            # duplicates emit dedup_hit (not the misleading record_failed)
            # AND other DB errors surface for the operator.
            try:
                ctx.dedup.record(
                    result,
                    applicant,
                    company,
                    role_title,
                    apply_url,
                )
            except AlreadyAppliedError:
                log.info(
                    "apply.dedup_hit",
                    extra={"ats": self.name, "reason": "record_race"},
                )
            except Exception:
                log.warning(
                    "apply.dedup.record_failed", extra={"ats": self.name}
                )
            log.info(
                "apply.submitted",
                extra={
                    "ats": self.name,
                    "application_id_present": bool(application_id),
                },
            )
            return result

        except Exception as exc:  # any unexpected error -> failed result
            log.info(
                "apply.failed",
                extra={"ats": self.name, "exc_type": type(exc).__name__},
            )
            try:
                screenshot = _take_screenshot(
                    page, _screenshot_path(ctx, "failed")
                )
                trace = _write_trace_placeholder(_trace_path(ctx, "failed"))
            except Exception:
                pass
            return _apply_result_failure(
                apply_url=apply_url or None,
                reason=f"unexpected error: {type(exc).__name__}",
                screenshot=screenshot,
                trace_path=trace,
            )
        finally:
            # L5: even though the caller owns the browser/context lifecycle
            # (transport-agnostic), we defensively call `close()` if page
            # exposes one. Playwright.Page.close() is idempotent, so this is
            # safe even when S17 or S10 also closes the outer context.
            close = getattr(page, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

