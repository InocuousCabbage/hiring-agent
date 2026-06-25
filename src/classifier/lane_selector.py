"""
classifier/lane_selector.py — Classify a JD into PMM, Content Marketing, or MOps.

Two-pass approach:
  Pass 1: Count keyword signal matches from config lanes.
           If top score >= 2x runner-up → clear winner, no API call needed.
  Pass 2: Ambiguous → call Claude (Haiku, cheap) as tiebreaker.
"""

import re

import structlog

from llm import call_claude

log = structlog.get_logger()

# Haiku is fast and cheap for this simple classification task
_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"


def classify_lane(jd_text: str, lanes_config: list[dict]) -> dict:
    """
    Classify a JD into exactly one lane.

    Returns the matching lane config dict:
      {"name": "pmm", "label": "Product Marketing (PMM)", "template": "...", ...}
    """
    scores = _score_by_signals(jd_text, lanes_config)
    top_lane, top_score = max(scores.items(), key=lambda x: x[1])
    sorted_scores = sorted(scores.values(), reverse=True)
    second_score = sorted_scores[1] if len(sorted_scores) > 1 else 0

    log.info("lane_selector.heuristic_scores", scores=scores)

    # Clear winner: top score is at least 2x the runner-up
    if top_score > 0 and top_score >= second_score * 2:
        lane = next(l for l in lanes_config if l["name"] == top_lane)
        log.info("lane_selector.heuristic_match", lane=lane["name"], score=top_score)
        return lane

    # Ambiguous — ask Claude
    log.info("lane_selector.ambiguous", scores=scores, calling_llm=True)
    lane_name = _classify_with_llm(jd_text, lanes_config)
    lane = next((l for l in lanes_config if l["name"] == lane_name), lanes_config[0])
    log.info("lane_selector.llm_match", lane=lane["name"])
    return lane


def _score_by_signals(jd_text: str, lanes_config: list[dict]) -> dict[str, int]:
    """Count signal keyword occurrences per lane (case-insensitive)."""
    jd_lower = jd_text.lower()
    return {
        lane["name"]: sum(
            len(re.findall(re.escape(signal.lower()), jd_lower))
            for signal in lane.get("signals", [])
        )
        for lane in lanes_config
    }


def _classify_with_llm(jd_text: str, lanes_config: list[dict]) -> str:
    """Use Claude Haiku to classify when keyword scores are ambiguous."""
    lane_descriptions = "\n".join(
        f"- {l['name']}: {l['label']} — signals: {', '.join(l.get('signals', []))}"
        for l in lanes_config
    )

    prompt = f"""Classify the following job description into exactly ONE of these lanes:

{lane_descriptions}

Reply with ONLY the lane name (pmm, content, or mops) — no explanation.

<job_description>
{jd_text[:3000]}
</job_description>"""

    raw = call_claude(prompt, model=_CLASSIFIER_MODEL)
    result = raw.strip().lower()
    valid = {l["name"] for l in lanes_config}

    if result not in valid:
        log.warning("lane_selector.llm_invalid_response", result=result)
        return lanes_config[0]["name"]

    return result
