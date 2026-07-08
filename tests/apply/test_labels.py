"""tests/apply/test_labels.py — RED tests for the S8 label-scan helper.

Every test here maps to one bullet in S8 spec §TDD test scaffolding /
Label helper (pure). The helper is deterministic, no I/O, no Playwright.

Landmine mapping:
    * L2/L11: `test_resolve_survives_greenhouse_renumbering` proves the resolver
      NEVER falls back to positional `select[name*="answers_attributes"]` — the
      selector index moves from _2_ to _7_ and the resolver still returns the
      right target.

The tests intentionally exercise both bare and BeautifulSoup-fed HTML shapes
because Greenhouse forms in the wild have wrapped labels, `for=` attributes,
and asterisks marking required fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.apply.adapters._labels import (  # noqa: E402  # import after sys.path tweak
    LabelledField,
    enumerate_questions,
    resolve,
)


# ── Fixture loader ────────────────────────────────────────────────────────────
FIXTURES = ROOT / "tests" / "fixtures" / "apply"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── resolve() ────────────────────────────────────────────────────────────────


def test_resolve_returns_for_attr_selector() -> None:
    html = '<label for="foo">Email</label><input id="foo">'
    assert resolve(html, "Email") == "#foo"


def test_resolve_is_case_insensitive() -> None:
    html = '<label for="foo">Email</label><input id="foo">'
    assert resolve(html, "email") == "#foo"
    assert resolve(html, "EMAIL") == "#foo"


def test_resolve_ignores_whitespace_and_asterisks() -> None:
    html = '<label for="fname">  First Name *  </label><input id="fname">'
    assert resolve(html, "first name") == "#fname"


def test_resolve_returns_none_when_no_match() -> None:
    html = '<label for="foo">Email</label><input id="foo">'
    assert resolve(html, "Cheese") is None


def test_resolve_handles_wrapped_label_without_for_attr() -> None:
    # No `for=` — label wraps the input directly.
    html = "<label>Email <input id=\"em\"></label>"
    assert resolve(html, "email") == "#em"


def test_resolve_survives_greenhouse_renumbering() -> None:
    """L2/L11: renumber the answers_attributes index; label-scan still resolves.

    The two-select fixture already exercises the L2/L11 anti-pattern shape.
    Here we swap answers_attributes_1_ -> answers_attributes_7_ and confirm the
    resolver still finds the Yes/No dropdown for visa sponsorship.
    """
    html = _load("greenhouse_form.html")
    renumbered = html.replace("answers_attributes_1_", "answers_attributes_7_")
    sel = resolve(renumbered, "Do you require visa sponsorship?")
    assert sel is not None
    assert sel.endswith("answers_attributes_7_answer")


# ── enumerate_questions() ────────────────────────────────────────────────────


def test_enumerate_finds_all_form_inputs() -> None:
    """Fixture defines 8 labelled inputs (see greenhouse_form.html):
    first_name, last_name, email, phone, resume, linkedin_url, and two selects.
    """
    fields = enumerate_questions(_load("greenhouse_form.html"))
    assert len(fields) == 8


def test_enumerate_captures_required_attribute() -> None:
    fields = enumerate_questions(_load("greenhouse_form.html"))
    by_label = {f.label.rstrip(" *").lower(): f for f in fields}
    assert by_label["first name"].required is True
    assert by_label["phone"].required is False


def test_enumerate_handles_wrapped_label() -> None:
    html = '<form id="application_form"><label>Email <input name="e" id="wr"></label></form>'
    fields = enumerate_questions(html)
    assert len(fields) == 1
    assert fields[0].label.lower() == "email"
    assert fields[0].selector == "#wr"


def test_enumerate_returns_labelled_field_dataclass() -> None:
    html = '<label for="e">Email *</label><input id="e" type="email" required>'
    fields = enumerate_questions(html)
    assert isinstance(fields[0], LabelledField)
    assert fields[0].input_type == "email"
    assert fields[0].required is True


def test_enumerate_captures_input_type_select() -> None:
    fields = enumerate_questions(_load("greenhouse_form.html"))
    selects = [f for f in fields if f.input_type == "select"]
    assert len(selects) == 2


def test_resolve_is_deterministic() -> None:
    """Idempotency: same HTML + same question -> same selector, twice."""
    html = _load("greenhouse_form.html")
    a = resolve(html, "Email")
    b = resolve(html, "Email")
    assert a == b == "#email"


def test_resolve_no_regex_on_raw_html() -> None:
    """L2/L11 discipline check: even a malformed-looking label pair should
    return None cleanly (no exception, no bogus match). This exercises the
    HTML-parser path rather than a regex on raw text.
    """
    html = "<label for=\"whatever\">Email is optional</label><input id=\"whatever\">"
    # Exact match is what we require; substring "Email" shouldn't leak through.
    assert resolve(html, "Email") is None
    assert resolve(html, "Email is optional") == "#whatever"
