"""
contacts/hm_finder.py — Find the hiring manager for a job posting.

Uses Claude Haiku with web_search server tool to search for the likely
hiring manager at each company, based on job title seniority and department.
Generates a short outreach note in the same API call.
"""

import json
import re

import structlog

from llm import call_claude

log = structlog.get_logger()

# ── Seniority mapping: job level → likely HM titles ─────────────────────────

_SENIORITY_MAP = {
    "intern": ["manager", "senior manager"],
    "coordinator": ["manager", "senior manager"],
    "specialist": ["manager", "senior manager", "director"],
    "analyst": ["manager", "senior manager", "director"],
    "associate": ["manager", "senior manager"],
    "manager": ["director", "senior director", "vp"],
    "senior manager": ["director", "senior director", "vp"],
    "director": ["vp", "senior vp", "svp", "chief"],
    "senior director": ["vp", "svp", "chief"],
    "vp": ["svp", "chief", "cmo", "cro"],
    "svp": ["chief", "cmo", "cro", "ceo"],
    "head of": ["vp", "svp", "chief", "cmo"],
    "lead": ["manager", "director", "head of"],
    "senior": ["manager", "director"],
}

# ── JD clue patterns ────────────────────────────────────────────────────────

_REPORTS_TO_RE = re.compile(
    r"(?:reports?\s+(?:directly\s+)?to|reporting\s+(?:directly\s+)?to)"
    r"\s+(?:the\s+)?([A-Z][A-Za-z\s,&]+?)(?:\.|,|\n|$)",
    re.IGNORECASE,
)

_HM_NAME_RE = re.compile(
    r"(?i:hiring\s+manager|contact)[:\s]+([A-Z][a-z]+(?: [A-Z][a-z]+)+)",
)


def extract_jd_clues(jd_text: str) -> dict:
    """Extract reporting structure and hiring manager clues from JD text."""
    clues = {"reports_to": None, "hm_name": None}

    match = _REPORTS_TO_RE.search(jd_text)
    if match:
        clues["reports_to"] = match.group(1).strip()

    match = _HM_NAME_RE.search(jd_text)
    if match:
        clues["hm_name"] = match.group(1).strip()

    return clues


def infer_hm_seniority(title: str) -> list[str]:
    """Given a job title, infer likely hiring manager title levels."""
    title_lower = title.lower()

    # Check longest keys first so "senior manager" matches before "manager"
    for level in sorted(_SENIORITY_MAP, key=len, reverse=True):
        if level in title_lower:
            return _SENIORITY_MAP[level]

    # Default: assume manager-level job → director+ HM
    return ["director", "vp", "head of"]


def _build_prompt(job: dict, jd_text: str, lane: str, clues: dict,
                  hm_titles: list[str], config: dict) -> str:
    """Build the prompt for the HM lookup API call."""
    clue_section = ""
    if clues["reports_to"]:
        clue_section += f"\nThe JD says this role reports to: {clues['reports_to']}"
    if clues["hm_name"]:
        clue_section += f"\nThe JD mentions a hiring manager name: {clues['hm_name']}"

    outreach_instruction = ""
    if config.get("generate_outreach_note", True):
        max_words = config.get("outreach_note_max_words", 60)
        outreach_instruction = (
            f"\n\nAlso write a short outreach note ({max_words} words max) that the "
            "candidate could send to this person on LinkedIn. The note should be "
            "conversational, mention the specific role, and reference one relevant "
            "skill or experience. Do not be generic or sycophantic."
        )

    return f"""Find the most likely hiring manager for this job posting.

<job>
Title: {job['title']}
Company: {job['company']}
Location: {job.get('location') or 'Not specified'}
Department/Lane: {lane}
</job>

<job_description_excerpt>
{jd_text[:2000]}
</job_description_excerpt>

<clues>{clue_section if clue_section else ' None found in JD'}
Likely HM title levels: {', '.join(hm_titles)}
</clues>

Search LinkedIn for the person at {job['company']} who is most likely the hiring manager \
for this role. Look for someone with a title like {', '.join(hm_titles)} in the \
{lane} or marketing department.

IMPORTANT: You MUST include the full LinkedIn profile URL (e.g. https://www.linkedin.com/in/username) \
for the person you identify. This is the most critical field. Search LinkedIn specifically to find it.{outreach_instruction}

Return ONLY valid JSON, no markdown fences:
{{
  "name": "<full name or null if not found>",
  "title": "<their current title or null>",
  "linkedin_url": "<LinkedIn profile URL or null>",
  "email": "<work email if discoverable, else null>",
  "confidence": "<high|medium|low>",
  "outreach_note": "<short personalized note or null>"
}}"""


def parse_hm_response(raw_text: str) -> dict | None:
    """Extract the HM info JSON from a Claude CLI plain-text response."""
    if not raw_text:
        return None

    raw = raw_text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # Try direct parse first, fall back to regex extraction
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        json_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL)
        if not json_match:
            log.warning("hm_finder.no_json_found", raw=raw[:300])
            return None
        raw = json_match.group(0)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("hm_finder.json_parse_error", error=str(e), raw=raw[:300])
            return None

    # Validate minimum fields
    if not result.get("name"):
        return None

    return {
        "name": result.get("name"),
        "title": result.get("title"),
        "linkedin_url": result.get("linkedin_url"),
        "email": result.get("email"),
        "confidence": result.get("confidence", "low") if result.get("confidence") in ("high", "medium", "low") else "low",
        "outreach_note": result.get("outreach_note"),
    }


def find_hiring_manager(
    job: dict,
    jd_text: str,
    lane: str,
    config: dict,
) -> dict | None:
    """
    Find the likely hiring manager for a job posting using web search.

    Args:
        job: Dict with title, company, location, url.
        jd_text: Full job description text.
        lane: Lane label (e.g. "Product Marketing (PMM)").
        config: The contacts config section from settings.yaml.

    Returns:
        Dict with name, title, linkedin_url, email, confidence, outreach_note
        or None if disabled or lookup fails.
    """
    if not config.get("enabled", False):
        log.info("hm_finder.disabled")
        return None

    model = config.get("model", "claude-haiku-4-5-20251001")
    timeout = config.get("timeout_seconds", 60)

    clues = extract_jd_clues(jd_text)
    hm_titles = infer_hm_seniority(job["title"])

    prompt = _build_prompt(job, jd_text, lane, clues, hm_titles, config)

    system = (
        "You are a recruiting researcher. Based on the job posting details, "
        "infer the most likely hiring manager. Be concise and return only JSON."
    )

    try:
        raw = call_claude(prompt, model=model, system=system, timeout=timeout)
    except Exception as e:
        log.warning("hm_finder.cli_error", error=str(e))
        return None

    return parse_hm_response(raw)
