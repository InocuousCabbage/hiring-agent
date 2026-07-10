"""
tailor/cover_letter.py — Generate a modern email-style cover letter.

3–4 short paragraphs, ~250 words, neutral/confident tone.
References 1–2 real projects from the bank with real metrics.
No links, no "Dear Hiring Manager", no boilerplate openers.
"""

import json
import re

import structlog

from llm import call_claude

log = structlog.get_logger()

_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You write modern, email-style cover letters for marketing professionals.

RULES (all non-negotiable):
  1. STRUCTURE — exactly 3–4 short paragraphs, ~250 words total.
  2. OPENER — start the first paragraph with something specific to the role or company.
     FORBIDDEN openers (do not start with or near these phrases):
       "I am writing to express", "I am excited to apply", "I believe I would be",
       "I am passionate about", "Please find enclosed", "Dear Hiring Manager",
       "I wanted to reach out", "I was thrilled to see"
     Instead: open with an observation about the company, the market, a challenge
     the role addresses, or a direct statement about your relevant work.
  3. PROJECTS — reference exactly 1–2 projects from the available project bank,
     using real metrics. No links or URLs ever.
  4. TRUTH ONLY — only reference real experience from the project bank.
     Never fabricate outcomes, titles, tools, or numbers.
  5. TONE — neutral and confident. No buzzwords ("synergy", "passionate", "leverage"),
     no filler phrases ("I would bring value"), no overselling.
  6. MIRROR THE JD — use the job description's language and priorities; don't ignore
     the role's core responsibilities.
  7. CLOSING — brief, direct. One sentence. No "I look forward to hearing from you
     at your earliest convenience."

CRITICAL — YOU MUST ALWAYS RETURN VALID JSON:
  Never write prose, explanations, or commentary instead of JSON — not even to flag a
  poor fit or raise a concern. If the role is a stretch, do your best with the available
  projects and note any mismatch inside a paragraph naturally. The pipeline handles fit
  decisions separately; your only job here is to produce the JSON structure below.

OUTPUT — return ONLY valid JSON, no markdown fences:
{
  "paragraphs": [
    "First paragraph — specific hook...",
    "Second paragraph — relevant experience + project/metrics...",
    "Third paragraph — additional value or second project...",
    "Fourth paragraph — direct closing (optional, include only if it adds substance)"
  ],
  "projects_referenced": ["proj_id_1", "proj_id_2"]
}"""


def write_cover_letter(
    jd_text: str,
    job: dict,
    lane: dict,
    project_bank: list[dict],
    config: dict,
) -> dict:
    """
    Generate a tailored cover letter.

    Returns:
      {"paragraphs": list[str], "projects_referenced": list[str]}
    """
    relevant_projects = [
        p for p in project_bank
        if lane["name"] in p.get("lane", [])
    ]

    prompt = f"""Write a cover letter for this position.

<job>
Title: {job['title']}
Company: {job['company']}
Location: {job.get('location') or 'Not specified'}
</job>

<job_description>
{jd_text[:3000]}
</job_description>

<lane>
{lane['label']}
</lane>

<available_projects>
{_format_projects(relevant_projects)}
</available_projects>

<constraints>
- Paragraphs: {config.get('max_paragraphs', 4)} max
- Projects to reference: {config.get('max_projects_referenced', 2)} max
- Tone: {config.get('tone', 'neutral')}
- ~250 words total
</constraints>

Return the cover letter as JSON."""

    raw = call_claude(prompt, model=_MODEL, system=SYSTEM_PROMPT).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("cover_letter.json_parse_error", error=str(e), raw=raw[:300])
        result = {
            "paragraphs": [
                f"Please find my application for the {job['title']} role at {job['company']}.",
                "My background in digital marketing, CRM integration, and data-driven campaign "
                "execution translates directly to the needs outlined in this position.",
                "I would welcome the opportunity to discuss how my experience aligns with your team's goals.",
            ],
            "projects_referenced": [],
        }

    result["paragraphs"] = [_clean_text(p) for p in result.get("paragraphs", [])]
    return result


def _clean_text(s: str) -> str:
    """Replace em/en-dashes then scrub the punctuation artifacts they leave behind."""
    s = s.replace("—", ", ").replace("–", "-")
    s = s.replace(" ,", ",")
    s = s.replace(" .", ".")
    s = re.sub(r"  +", " ", s)
    s = s.replace(",  ", ", ")
    return s.strip()


def _format_projects(projects: list[dict]) -> str:
    if not projects:
        return "(No projects available)"

    lines = []
    for p in projects:
        lines.append(f"[{p.get('id', '?')}] {p['name']} — {p.get('company', '')}")
        lines.append(f"  {p.get('summary', '').strip()}")
        for m in p.get("metrics", []):
            lines.append(f"  📊 {m}")
        lines.append("")

    return "\n".join(lines)
