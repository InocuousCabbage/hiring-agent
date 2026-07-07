"""Shared label-scan helper for ATS form adapters (Greenhouse / Lever / Ashby).

The two-layer split (per variation-A + S8 spec) requires a PURE HTML label
resolver — no Playwright, no I/O, no state. This module is that helper.

Contract summary:
    * `resolve(html, question_text) -> str | None`
        Deterministic label-scan: match a `<label>` on question_text
        (case-insensitive, whitespace/asterisk-tolerant, exact-token match),
        then return the associated input's CSS selector (`#id` when possible,
        else `[name='...']`). Returns None on no match.

    * `enumerate_questions(html) -> list[LabelledField]`
        Every `(label, selector, input_type, required, name_attr)` triple in
        the form. Used by the Greenhouse planner and by test fixtures.

    * `FieldFill` (frozen dataclass) — return-element of `plan_form_fill()`.
        Owned by S8 per spec §Contracts produced / §File ownership. (S2's
        initial types.py referenced a leaner FieldFill; the S8 shape is the
        one the whole apply pipeline should converge on.)

Landmine discipline:
    * L2/L11: this helper is the ONLY approved way to resolve a Greenhouse
      "answers_attributes" input to a selector. Never hardcode
      `select[name*='answers_attributes']` first-match anywhere.
    * The parser is BeautifulSoup — NOT a regex on raw HTML. Regex on raw
      HTML would be a code-review BLOCKER (spec §Code-review pass criteria).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from bs4 import BeautifulSoup, Tag


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldFill:
    """One planned field-fill in the pure-planner phase (variation-A).

    Consumed by `GreenhouseAdapter.apply()` — every field the driver executes
    starts life as a FieldFill. Frozen so the planner output is safe to reuse
    across a retry or a Gmail-review-loop replay (S12).

    Fields:
        selector: CSS selector (`#id` preferred, else `[name='...']`) the
            driver will target.
        strategy: How the driver executes this fill. `select_option_by_label`
            is the L4-compliant path for `<select>` — never positional value.
        value: Payload — string, bool, or Path.
        label: Human question text (resolved from `<label>` in enumerate).
        required: Whether the source form marked the field as required.
        source: Provenance — `boards_api` when the Greenhouse Boards API
            provided the schema; `label_scan` when we fell back to DOM
            introspection; `fallback` reserved for future hardcoded rescue.
    """

    selector: str
    strategy: Literal["fill", "select_option_by_label", "check", "upload"]
    value: Any
    label: str
    required: bool
    source: Literal["boards_api", "label_scan", "fallback"]


@dataclass(frozen=True)
class LabelledField:
    """One (label -> input) pair from `enumerate_questions(html)`."""

    label: str
    selector: str
    input_type: Literal[
        "text", "email", "tel", "select", "checkbox", "radio", "textarea", "file"
    ]
    required: bool
    name_attr: str | None


# ── Internals ────────────────────────────────────────────────────────────────


_TRAILING_ASTERISK_RE = re.compile(r"\s*\*+\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_question(s: str) -> str:
    """Lowercase, strip trailing asterisks, collapse whitespace."""
    if s is None:
        return ""
    s = _TRAILING_ASTERISK_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s.strip())
    return s.lower()


def _input_type_for(tag: Tag) -> str:
    name = (tag.name or "").lower()
    if name == "textarea":
        return "textarea"
    if name == "select":
        return "select"
    t = (tag.get("type") or "text").lower()
    if t in {"email", "tel", "checkbox", "radio", "file"}:
        return t
    return "text"


def _selector_for(tag: Tag) -> str:
    """Prefer `#id`; else `[name='...']`; else the raw tag name as last resort."""
    tid = tag.get("id")
    if tid:
        return f"#{tid}"
    name = tag.get("name")
    if name:
        return f"[name='{name}']"
    return tag.name or ""


def _label_target(label_tag: Tag, soup: BeautifulSoup) -> Tag | None:
    """Given a `<label>`, return the input/select/textarea it targets.

    Tries `for=` first; falls back to a nested input; returns None if neither.
    """
    for_id = label_tag.get("for")
    if for_id:
        target = soup.find(id=for_id)
        if isinstance(target, Tag):
            return target
    # Fall back to nested input/select/textarea.
    nested = label_tag.find(["input", "select", "textarea"])
    if isinstance(nested, Tag):
        return nested
    return None


def _label_text_for(tag: Tag) -> str | None:
    """Return the direct visible text of a `<label>` (nested inputs stripped)."""
    parts: list[str] = []
    for content in tag.contents:
        # Skip nested inputs so their value doesn't bleed into the label.
        if isinstance(content, Tag) and content.name in {
            "input",
            "select",
            "textarea",
            "button",
        }:
            continue
        parts.append(str(getattr(content, "text", content)))
    return " ".join(p for p in (s.strip() for s in parts) if p) or None


# ── Public API ───────────────────────────────────────────────────────────────


def resolve(html: str, question_text: str) -> str | None:
    """Locate the CSS selector for the input associated with `question_text`.

    Deterministic: identical (html, question_text) inputs yield identical
    output. Never raises — malformed HTML returns None.

    Landmine L2/L11: this is the ONLY approved resolution path for
    Greenhouse `answers_attributes` inputs. Do not first-match on a
    `select[name*=]` selector anywhere in adapters.
    """
    if not html or not question_text:
        return None

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover — bs4 falls back to html.parser normally
        soup = BeautifulSoup(html, "html.parser")

    target_norm = _normalize_question(question_text)
    if not target_norm:
        return None

    for label in soup.find_all("label"):
        if not isinstance(label, Tag):
            continue
        label_text = _label_text_for(label)
        if label_text is None:
            continue
        if _normalize_question(label_text) != target_norm:
            continue
        target = _label_target(label, soup)
        if target is None:
            continue
        return _selector_for(target)
    return None


def enumerate_questions(html: str) -> list[LabelledField]:
    """Every labelled input in `html`, in document order.

    Non-labelled inputs are skipped by design — the adapter should never
    fill a field it can't attribute to a human question.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover
        soup = BeautifulSoup(html, "html.parser")

    out: list[LabelledField] = []
    seen_targets: set[int] = set()

    for label in soup.find_all("label"):
        if not isinstance(label, Tag):
            continue
        target = _label_target(label, soup)
        if target is None:
            continue
        target_id = id(target)
        if target_id in seen_targets:
            continue
        seen_targets.add(target_id)
        label_text = _label_text_for(label) or ""
        if not label_text:
            continue
        out.append(
            LabelledField(
                label=label_text,
                selector=_selector_for(target),
                input_type=_input_type_for(target),  # type: ignore[arg-type]
                required=target.has_attr("required"),
                name_attr=target.get("name"),
            )
        )
    return out
