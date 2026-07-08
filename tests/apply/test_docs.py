"""Grep-based acceptance tests for the Phase 3 auto-apply MVP docs shard (S21).

These tests exist so a reviewer can verify — without reading prose — that the
three docs (README.md, docs/apply-flow.md, SETUP.md) contain the required
content, references, and structural properties spelled out in
`.agent/one-big-feature/auto-apply-2026-07-06/03-specs/21-s21-docs.md`.

They are RED before the docs land and GREEN once the docs match spec.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
APPLY_FLOW = REPO_ROOT / "docs" / "apply-flow.md"
SETUP = REPO_ROOT / "SETUP.md"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

ALL_DOCS = (README, APPLY_FLOW, SETUP)

REQUIRED_H2_ORDER = [
    "## Overview",
    "## Prerequisites",
    "## Bootstrap",
    "## Configuration",
    "## Pipeline flow",
    "## Review mode (default)",
    "## Dedup semantics",
    "## Rate limiting",
    "## CAPTCHA handling",
    "## Computer Use fallback (opt-in)",
    "## Retention",
    "## Logging + PII",
    "## Live testing",
    "## Success criteria for enabling dry_run: false",
    "## Out of scope",
    "## Troubleshooting",
]

CANONICAL_PATHS = [
    "config/settings.yaml",
    "state/applied_jobs.db",
    "state/traces/",
    "state/screenshots/",
    "config/credentials/apply/",
    "templates/candidate_profile.yaml",
]


def _read(path: Path) -> str:
    assert path.exists(), f"expected doc missing: {path}"
    return path.read_text(encoding="utf-8")


def _section(text: str, h2: str) -> str:
    """Return the body of an H2 section (up to the next H2 or EOF)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == h2:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    return "\n".join(lines[start:end])


# --- README --------------------------------------------------------------


def test_readme_mentions_auto_apply():
    body = _read(README)
    assert "opt-in, review-mode default" in body
    assert "Greenhouse only" in body
    assert "docs/apply-flow.md" in body
    assert "apply.enabled=true" in body


def test_readme_has_auto_apply_section_h2():
    body = _read(README)
    matches = re.findall(r"^## Auto-Apply \(Phase 3 MVP\)\s*$", body, flags=re.M)
    assert len(matches) == 1, (
        f"expected exactly one '## Auto-Apply (Phase 3 MVP)' heading, got {len(matches)}"
    )


# --- apply-flow.md structure --------------------------------------------


def test_apply_flow_all_required_h2_sections_present():
    body = _read(APPLY_FLOW)
    lines = body.splitlines()
    idx = 0
    for expected in REQUIRED_H2_ORDER:
        found_at = None
        for j in range(idx, len(lines)):
            if lines[j].strip() == expected:
                found_at = j
                break
        assert found_at is not None, (
            f"missing or out-of-order H2 heading: {expected!r} "
            f"(scanning from line {idx})"
        )
        idx = found_at + 1


def test_apply_flow_bootstrap_section_names_command():
    section = _section(_read(APPLY_FLOW), "## Bootstrap")
    assert "python -m src.apply.bootstrap greenhouse" in section
    assert "python -m src.apply.bootstrap --status" in section
    assert "hiring-agent.<ats>.<user>" in section
    assert "0o700" in section
    assert "0o600" in section
    assert "5-min" in section or "5 min" in section or "five-minute" in section


def test_apply_flow_review_mode_documents_yes_no_parser():
    section = _section(_read(APPLY_FLOW), "## Review mode (default)")
    assert "hiring-agent/apply/pending" in section
    assert "hiring-agent/apply/submitted" in section
    assert "hiring-agent/apply/declined" in section
    assert "YES" in section
    assert "NO" in section
    assert "24" in section
    assert "72" in section
    assert "please reply YES or NO on the first line" in section


def test_apply_flow_dedup_documents_hard_and_soft():
    section = _section(_read(APPLY_FLOW), "## Dedup semantics")
    assert "UNIQUE(company, ats_domain, ats_job_id)" in section
    assert "company_normalized" in section
    assert "role_title_normalized" in section
    assert "python -m src.apply.dedup --unblock" in section
    assert "Inc|LLC|Corp|Ltd|GmbH" in section
    assert "Sr|Senior|Jr|Junior|Staff|Principal|Lead" in section


def test_apply_flow_captcha_names_five_kinds():
    section = _section(_read(APPLY_FLOW), "## CAPTCHA handling")
    for name in (
        "Cloudflare Turnstile",
        "reCAPTCHA v2",
        "reCAPTCHA v3",
        "hCaptcha",
        "DataDome",
        "solve_captchas",
        "proxies",
    ):
        assert name in section, f"CAPTCHA section missing {name!r}"


def test_apply_flow_computer_use_marked_review_required_L13():
    section = _section(_read(APPLY_FLOW), "## Computer Use fallback (opt-in)")
    has_phrase = (
        "HARD-CODED to review_required" in section
        or "**hard-coded to `review_required`**" in section
    )
    assert has_phrase, "Computer Use section must surface L13 review_required rule"
    assert "apply.long_tail: computer_use" in section
    assert "eligibility hallucination" in section.lower()


def test_apply_flow_success_criteria_reproduces_six_checks():
    section = _section(
        _read(APPLY_FLOW), "## Success criteria for enabling dry_run: false"
    )
    for n in ("1.", "2.", "3.", "4.", "5.", "6."):
        assert re.search(rf"^\s*{re.escape(n)} ", section, flags=re.M), (
            f"success-criteria section missing numbered item {n!r}"
        )
    assert "boards.greenhouse.io/greenhouse" in section
    assert "python -m src.apply.bootstrap" in section
    assert "pytest tests/apply/" in section
    assert "pytest -m live_ats" in section
    assert "7-day soak" in section or "7 day soak" in section


def test_apply_flow_out_of_scope_lists_locked_items():
    section = _section(_read(APPLY_FLOW), "## Out of scope")
    for item in (
        "LinkedIn Easy Apply",
        "_ALLOWED_COMPANIES",
        "multi-user",
        "post-submit",
        "salary negotiation",
        "dashboard",
    ):
        assert item in section, f"out-of-scope section missing {item!r}"


def test_apply_flow_troubleshooting_has_four_recipes():
    section = _section(_read(APPLY_FLOW), "## Troubleshooting")
    subsections = re.findall(r"^### .+$", section, flags=re.M)
    assert len(subsections) >= 4, (
        f"expected >= 4 troubleshooting ### subsections, got {len(subsections)}"
    )
    joined = "\n".join(subsections).lower()
    assert "session" in joined and "expired" in joined
    assert "review" in joined
    assert "unblock" in joined or "duplicate" in joined
    assert "captcha" in joined


# --- SETUP.md -----------------------------------------------------------


def test_setup_has_auto_apply_section():
    body = _read(SETUP)
    assert "## Auto-apply setup" in body
    assert "python -m playwright install chromium" in body
    assert "python -m src.apply.bootstrap greenhouse" in body
    assert "HIRING_AGENT_LIVE_ATS" in body


def test_setup_does_not_add_new_pip_deps():
    body = _read(SETUP)
    section_lines = []
    inside = False
    for line in body.splitlines():
        if line.strip() == "## Auto-apply setup":
            inside = True
            continue
        if inside and line.startswith("## ") and not line.startswith("### "):
            break
        if inside:
            section_lines.append(line)
    section = "\n".join(section_lines)
    reqs = REQUIREMENTS.read_text(encoding="utf-8").lower()
    # Extract every `pip install <arg>` occurrence in the auto-apply section.
    for match in re.finditer(r"pip install\s+([^\s`\n]+)", section):
        arg = match.group(1).strip("`")
        if arg in ("-r", "--upgrade", "-U"):
            continue
        if arg.startswith("-"):
            continue
        # If the arg is a requirements-file reference, allow it.
        if arg.endswith(".txt"):
            continue
        # Otherwise it must name a package already in requirements.txt.
        pkg = re.split(r"[<>=!~\[]", arg, maxsplit=1)[0].strip().lower()
        assert pkg and pkg in reqs, (
            f"SETUP.md auto-apply section installs {arg!r}, "
            "which is not in requirements.txt"
        )


# --- cross-doc invariants -----------------------------------------------


def test_docs_reference_same_paths():
    combined = "\n".join(_read(p) for p in ALL_DOCS)
    for path in CANONICAL_PATHS:
        assert path in combined, (
            f"canonical path {path!r} not referenced in any of the three docs"
        )


def test_docs_have_no_pii():
    pattern = re.compile(r"benjrocks2|benjoslin52", re.IGNORECASE)
    for path in ALL_DOCS:
        body = _read(path)
        assert not pattern.search(body), (
            f"PII string leaked into {path.relative_to(REPO_ROOT)}"
        )


def test_docs_have_no_emoji():
    # Spec-specified regex range covering the emoji plane + dingbats.
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
    for path in ALL_DOCS:
        body = _read(path)
        m = emoji_re.search(body)
        assert m is None, (
            f"emoji {m.group(0)!r} in {path.relative_to(REPO_ROOT)} "
            "violates persona rule"
        )


def test_docs_have_no_broken_relative_links():
    # [text](href) — skip images, anchors, external urls, mailto.
    link_re = re.compile(r"(?<!\!)\[[^\]]+\]\(([^)]+)\)")
    for path in ALL_DOCS:
        body = _read(path)
        for match in link_re.finditer(body):
            href = match.group(1).strip()
            if not href:
                continue
            if href.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Drop any in-doc anchor suffix.
            target = href.split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.exists(), (
                f"broken relative link in {path.relative_to(REPO_ROOT)}: "
                f"{href!r} -> {resolved}"
            )


def test_docs_ascending_heading_levels():
    """No line starts with `####` without a preceding `###` under the same `##`."""
    for path in ALL_DOCS:
        seen_h3 = False
        for i, line in enumerate(_read(path).splitlines(), start=1):
            if line.startswith("## ") and not line.startswith("### "):
                seen_h3 = False
            elif line.startswith("### ") and not line.startswith("#### "):
                seen_h3 = True
            elif line.startswith("#### "):
                assert seen_h3, (
                    f"{path.relative_to(REPO_ROOT)}:{i} jumps to H4 "
                    "without an H3 under the current H2"
                )


def test_docs_no_unmatched_code_fences():
    for path in ALL_DOCS:
        body = _read(path)
        fence_count = len(re.findall(r"^```", body, flags=re.M))
        assert fence_count % 2 == 0, (
            f"{path.relative_to(REPO_ROOT)} has {fence_count} ``` fence lines "
            "(must be even)"
        )
