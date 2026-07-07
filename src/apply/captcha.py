"""S9: CAPTCHA / bot-wall detection for the auto-apply pipeline.

Pure DOM-marker classifier — no vision, no network, no filesystem, no page
navigation.  Called by adapters (S8) before submit and by the Browserbase
transport (S10) after a solve attempt.  The result drives the escalation
branch documented in master-plan §4.7 (`apply.captcha_transport`).

Detection is ordered — first match wins.  The order is:

    1. cloudflare_turnstile
    2. recaptcha_v2
    3. recaptcha_v3
    4. hcaptcha
    5. datadome

Precedence rules baked into the ordering:

* reCAPTCHA v2 takes precedence over v3.  A page carrying both the v2 checkbox
  widget and the v3 invisible/render markers is treated as v2 (the interactive
  challenge is the blocking one).
* Any Turnstile marker outranks every downstream kind — Cloudflare walls are
  typically the outermost layer when multiple stacks appear on the same page.

The detector is transport- and ATS-agnostic.  It never mentions Greenhouse,
Lever, Ashby, Workday, or any other ATS by name (landmine L14) and never
uses text-matching selectors (landmine L1) — only structural DOM selectors
guarded by ``locator(sel).count() > 0`` so a missing marker can never raise.

Logging: on a positive match a single ``apply.captcha_detected`` structlog
event is emitted with keys ``kind`` and ``page_url`` only.  No ``data-sitekey``,
no cookies, no field values — the PII-scrub concern (landmine L7) is satisfied
by construction because those values are simply never passed to the logger.
Zero events are emitted when the detector returns ``None``.
"""

from __future__ import annotations

from typing import Literal

import structlog
from playwright.sync_api import Page

__all__ = ["CaptchaKind", "detect"]


CaptchaKind = Literal[
    "cloudflare_turnstile",
    "recaptcha_v2",
    "recaptcha_v3",
    "hcaptcha",
    "datadome",
]

# Ordered detection sequence — first match wins.  Keep in sync with
# ``typing.get_args(CaptchaKind)`` (the ``test_captcha_kind_is_closed_literal``
# test locks the order).
_ORDER: tuple[CaptchaKind, ...] = (
    "cloudflare_turnstile",
    "recaptcha_v2",
    "recaptcha_v3",
    "hcaptcha",
    "datadome",
)

# Per-kind selector palette.  Semantics: ANY selector in the tuple matching
# (``count() > 0``) is enough to classify the page as that kind.
#
# Notes on individual entries:
#   * reCAPTCHA v2 — the invisible variant sets ``data-size="invisible"`` and
#     is a v3-shape widget, so we EXCLUDE ``[data-size="invisible"]`` here to
#     avoid double-classifying v3 pages as v2.
#   * reCAPTCHA v3 — the render script or an invisible ``g-recaptcha`` div
#     is the giveaway; because v2 is checked FIRST in ``_ORDER`` this rule is
#     only reached when no visible v2 widget is present, so precedence is
#     enforced by control flow rather than by a negation in the selector.
#   * DataDome — the visible ``#ddv1-captcha-container`` covers the tags.js
#     + visible-container composite from spec §Acceptance #6; DataDome only
#     injects that container when actively blocking, so its presence alone
#     is a reliable positive.
_SELECTORS: dict[CaptchaKind, tuple[str, ...]] = {
    "cloudflare_turnstile": (
        "iframe[src*='challenges.cloudflare.com/turnstile']",
        "div.cf-turnstile",
        "[data-sitekey][data-callback][class*='turnstile']",
    ),
    "recaptcha_v2": (
        # The anchor iframe URL carries a `size=` param; v3 stamps it
        # `size=invisible`.  Excluding that variant here prevents a v3-only
        # page from being misclassified as v2 once Google's api.js executes
        # and injects the widget.
        "iframe[src*='google.com/recaptcha/api2/anchor']:not([src*='size=invisible'])",
        "div.g-recaptcha[data-sitekey]:not([data-size='invisible'])",
    ),
    "recaptcha_v3": (
        "script[src*='google.com/recaptcha/api.js?render=']",
        "div.g-recaptcha[data-size='invisible']",
    ),
    "hcaptcha": (
        "iframe[src*='hcaptcha.com']",
        "div.h-captcha[data-sitekey]",
        "[data-hcaptcha-widget-id]",
    ),
    "datadome": (
        "#ddg-captcha-wrapper",
        "iframe[src*='captcha-delivery.com']",
        "#ddv1-captcha-container:visible",
    ),
}

_LOGGER = structlog.get_logger(__name__)


def detect(page: Page) -> CaptchaKind | None:
    """Return the CAPTCHA/bot-wall kind gating ``page``, or ``None``.

    Pure DOM introspection — the function performs only ``page.locator(sel).count()``
    reads.  No navigation, no reload, no ``page.evaluate``, no network I/O, no
    filesystem access.  Each selector is guarded by ``count() > 0`` so a missing
    marker returns cleanly instead of raising.

    On a positive match a single ``apply.captcha_detected`` structlog event is
    emitted (keys: ``kind``, ``page_url``).  On ``None`` no event is emitted.
    """
    for kind in _ORDER:
        if any(page.locator(sel).count() > 0 for sel in _SELECTORS[kind]):
            _LOGGER.info(
                "apply.captcha_detected",
                kind=kind,
                page_url=page.url,
            )
            return kind
    return None
