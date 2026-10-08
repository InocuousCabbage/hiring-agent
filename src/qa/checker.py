"""
qa/checker.py — Validate tailored resume JSON before rendering.

run_qa() performs rule-based checks; returns {"pass": bool, "errors": list[str],
"copy_errors": list[dict]} (copy_errors is the copy-gate subset of errors).
Warnings are logged but do not cause failure.
auto_fix() asks Claude to fix any errors in place.
"""

import json
import re

import structlog

from llm import call_claude
from src.utils.employers import get_allowed_employers

from .copy_gate import copy_fields, scan_fields

log = structlog.get_logger()

# Companies the user actually worked at are NOT listed here. They are read at
# runtime from the gitignored config/settings.local.yaml (top-level
# `employers:` list plus user.company) by src/utils/employers.py, which fails
# loud when the list is missing or empty. Any other employer name in a bullet
# is treated as a possible fabrication.

# Role indices that are allowed to be edited
_ALLOWED_ROLE_INDICES = frozenset({0, 1, 2, 3})

# Patterns that signal "robot resume" language (warnings, not errors)
_ROBOT_PHRASES = [
    r"results-driven professional",
    r"detail-oriented team player",
    r"proven track record of success",
    r"passionate about leveraging",
    r"synergy",
    r"dynamic individual",
    r"go-getter",
]

# Regex to detect URLs in bullets
_URL_RE = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)

# Regex to detect corporate entity suffixes (signals a possibly fabricated company)
_CORP_SUFFIX_RE = re.compile(r'(?<!\w)(?:(?:Inc|Corp|LLC|Ltd)\.?|Co\.)(?!\w)', re.IGNORECASE)

# Organisation-style names without a legal suffix ("Bluefin Labs", "Harbor
# Group"): a capitalised word directly followed by a capitalised org noun.
# Case-sensitive on purpose, so "lab results" or "a focus group" never match.
# Nouns that commonly follow a PRODUCT name are left out ("Analytics",
# "Cloud", "Studio" singular, "Systems"), because "Google Analytics" in a
# bullet is a tool, not an employer.
#
# KNOWN GAP, stated rather than implied: a bare invented name with neither a
# legal suffix nor an org noun ("At Bluefin, I rebuilt...") still passes.
# Catching that needs a list of every capitalised token that is allowed
# (tools, places, products), and guessing at it would flag ordinary
# capitalised words in most bullets. The allow-list check below is only as
# good as these two triggers.
_ORG_NAME_RE = re.compile(
    r"\b[A-Z][\w&'-]*\s+(?:Labs|Group|Holdings|Partners|Ventures|Technologies|"
    r"Agency|Studios|Consulting|Corporation|Incorporated|GmbH|PLC|LLP)\b"
)


def run_qa(
    tailored_resume: dict,
    cover_letter: dict,
    jd_text: str,
    lane: dict,
    config: dict,
) -> dict:
    """
    Validate tailored resume JSON and cover letter.

    Returns {"pass": bool, "errors": list[str], "copy_errors": list[dict]}.
    Errors are hard failures; warnings are logged only. copy_errors holds the
    copy-gate errors (rule_id, matched, field, message); each message is also
    in errors.
    """
    errors: list[str] = []
    warnings: list[str] = []
    # Loaded (and cached) on every call, not only when a bullet looks like it
    # names a company, so a missing list fails on the first job, not on
    # whichever job first happens to mention an employer.
    allowed_companies = get_allowed_employers()

    summary = tailored_resume.get("summary", "")
    skills  = tailored_resume.get("skills", [])
    roles   = tailored_resume.get("roles", [])

    # ── Resume checks ─────────────────────────────────────────────────────────

    # 1. Summary non-empty and under 350 chars
    if not summary:
        errors.append("summary is empty")
    elif len(summary) > 350:
        errors.append(f"summary exceeds 350 chars ({len(summary)})")

    # 2. Exactly 9 skills (3×3 table)
    if len(skills) != 9:
        errors.append(f"expected exactly 9 skills, got {len(skills)}")

    # 3. Roles list uses only indices 0, 1, 2
    null_roles = [r for r in roles if r.get("index") is None]
    if null_roles:
        errors.append(
            f"{len(null_roles)} role(s) have null index — each role must have an integer index in {sorted(_ALLOWED_ROLE_INDICES)}"
        )
    valid_roles = [r for r in roles if r.get("index") is not None]
    role_indices = {r["index"] for r in valid_roles}
    bad_indices = role_indices - _ALLOWED_ROLE_INDICES
    if bad_indices:
        errors.append(f"role indices outside allowed set {set(sorted(_ALLOWED_ROLE_INDICES))}: {sorted(bad_indices)}")
    if not roles:
        errors.append("roles list is empty")

    # 4. Each bullet under 250 chars; no URLs; no fabricated companies
    for role in valid_roles:
        for bullet in role.get("bullets", []):
            if len(bullet) > 250:
                errors.append(f"bullet exceeds 250 chars ({len(bullet)}): '{bullet[:60]}...'")
            if _URL_RE.search(bullet):
                errors.append(f"bullet contains a URL: '{bullet[:80]}'")
            if _CORP_SUFFIX_RE.search(bullet) or _ORG_NAME_RE.search(bullet):
                # Check if the surrounding context matches an allowed company
                if not any(c in bullet.lower() for c in allowed_companies):
                    errors.append(
                        f"bullet may reference a fabricated company name: '{bullet[:80]}'"
                    )

    # 5. keywords_integrated non-empty
    if not tailored_resume.get("keywords_integrated"):
        errors.append("keywords_integrated is empty")

    # ── Cover letter checks ───────────────────────────────────────────────────

    paragraphs = cover_letter.get("paragraphs", [])
    if len(paragraphs) < 2:
        errors.append(f"cover letter has only {len(paragraphs)} paragraph(s); need at least 2")

    max_paras = config.get("cover_letter", {}).get("max_paragraphs", 4)
    if len(paragraphs) > max_paras:
        errors.append(f"cover letter has {len(paragraphs)} paragraphs (max {max_paras})")

    # ── Copy-audit gate (vendored copy_scan rules) ───────────────────────────
    # Error-level hits are QA errors, so the auto_fix retry loop sees them.
    # They are also returned separately under "copy_errors" so run_pipeline
    # can tell a copy-only failure from any other QA failure after retries.
    copy_error_hits, copy_warn_hits = scan_fields(copy_fields(tailored_resume, cover_letter))
    copy_errors = [
        {"rule_id": h.rule_id, "matched": h.matched, "field": h.field, "message": h.message}
        for h in copy_error_hits
    ]
    errors.extend(c["message"] for c in copy_errors)
    for h in copy_warn_hits:
        log.warning("qa.copy_warning", rule_id=h.rule_id, matched=h.matched, field=h.field)

    # ── Warnings (logged, not blocking) ──────────────────────────────────────

    all_text = " ".join([summary] + [b for r in roles for b in r.get("bullets", [])]
                        + paragraphs)
    for phrase in _ROBOT_PHRASES:
        if re.search(phrase, all_text, re.IGNORECASE):
            warnings.append(f"robot language detected: '{phrase}'")

    # ── Log and return ────────────────────────────────────────────────────────

    passed = len(errors) == 0
    log.info(
        "qa.result",
        passed=passed,
        errors=len(errors),
        warnings=len(warnings),
    )
    for e in errors:
        log.error("qa.error", detail=e)
    for w in warnings:
        log.warning("qa.warning", detail=w)

    return {"pass": passed, "errors": errors, "copy_errors": copy_errors}


def auto_fix(
    tailored_resume: dict,
    cover_letter: dict,
    issues: list[str],
    jd_text: str,
    lane: dict,
    project_bank: list[dict],
) -> tuple[dict, dict]:
    """
    Ask Claude to fix the identified QA errors.
    Returns (fixed_resume, fixed_cover_letter).
    """
    issues_text = "\n".join(f"- {e}" for e in issues)

    prompt = f"""The following resume and cover letter have quality issues that must be fixed.

<issues>
{issues_text}
</issues>

<current_resume>
{json.dumps(tailored_resume, indent=2)}
</current_resume>

<current_cover_letter>
{json.dumps(cover_letter, indent=2)}
</current_cover_letter>

<job_description>
{jd_text[:2000]}
</job_description>

Fix ALL issues while maintaining content quality.

HARD CONSTRAINTS:
- Every role in "roles" MUST have "index" set to an integer: 0, 1, 2, or 3. Never null or missing.
- "skills" MUST be a list of exactly 9 strings.
- "summary" MUST be under 350 characters.
- A "copy check failed: RULE: 'text' in FIELD" issue means FIELD contains a banned
  pattern. Rewrite that sentence so the pattern is gone; do not just delete the
  punctuation and leave a broken sentence.

Return a JSON object:
{{
  "resume": {{ ...same structure as current_resume... }},
  "cover_letter": {{ ...same structure as current_cover_letter... }}
}}

Only return valid JSON. No explanation."""

    raw = call_claude(prompt, model="claude-sonnet-4-6").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        fixed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("qa.auto_fix_parse_error", error=str(e))
        return tailored_resume, cover_letter

    # L1 (Phase 6 audit + iter-2 refinement): shape guard covers both the
    # ROOT and each sub-value. Pre-fix: `fixed_resume["lane"] = ...` raised
    # TypeError when a sub-value wasn't a dict. Iter-1 fix: guard sub-values.
    # Iter-2 fix: also guard the ROOT — a non-object JSON root
    # (`raw = "[]"` or `"\"foo\""`) makes `fixed.get(...)` raise
    # AttributeError, which was NOT caught by the JSONDecodeError branch
    # above. Any non-dict root → fall back to originals.
    if not isinstance(fixed, dict):
        log.warning("qa.auto_fix_wrong_root_shape", got=type(fixed).__name__)
        return tailored_resume, cover_letter
    fixed_resume = fixed.get("resume", tailored_resume)
    fixed_cl = fixed.get("cover_letter", cover_letter)
    if not isinstance(fixed_resume, dict):
        log.warning("qa.auto_fix_wrong_resume_shape", got=type(fixed_resume).__name__)
        fixed_resume = tailored_resume
    if not isinstance(fixed_cl, dict):
        log.warning("qa.auto_fix_wrong_cover_letter_shape", got=type(fixed_cl).__name__)
        fixed_cl = cover_letter
    # Preserve the lane on the (now-dict-guaranteed) tailored resume.
    if fixed_resume is not tailored_resume:
        fixed_resume["lane"] = tailored_resume.get("lane")
    log.info("qa.auto_fix_applied")
    return fixed_resume, fixed_cl
