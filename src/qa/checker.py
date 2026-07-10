"""
qa/checker.py — Validate tailored resume JSON before rendering.

run_qa() performs rule-based checks; returns {"pass": bool, "errors": list[str]}.
Warnings are logged but do not cause failure.
auto_fix() asks Claude to fix any errors in place.
"""

import json
import re

import structlog

from llm import call_claude

log = structlog.get_logger()

# Companies the user actually worked at — any other employer name in bullets is a fabrication.
# Update this list with YOUR real employers.
_ALLOWED_COMPANIES = frozenset({
    "acme corp",
    "example consulting",
    "freelance",
})

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


def run_qa(
    tailored_resume: dict,
    cover_letter: dict,
    jd_text: str,
    lane: dict,
    config: dict,
) -> dict:
    """
    Validate tailored resume JSON and cover letter.

    Returns {"pass": bool, "errors": list[str]}.
    Errors are hard failures; warnings are logged only.
    """
    errors: list[str] = []
    warnings: list[str] = []

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
            if _CORP_SUFFIX_RE.search(bullet):
                # Check if the surrounding context matches an allowed company
                if not any(c in bullet.lower() for c in _ALLOWED_COMPANIES):
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

    return {"pass": passed, "errors": errors}


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

    # L1 (Phase 6 audit): shape guard — valid JSON with wrong root shape
    # (e.g. {"resume": [], "cover_letter": []}) previously raised TypeError
    # at `fixed_resume["lane"] = ...` and escaped the try/except. Fall back
    # to originals on either side that isn't a dict, so a malformed LLM
    # response never crashes the tailoring pipeline.
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
