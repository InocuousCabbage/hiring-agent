#!/usr/bin/env python3
"""
tests/test_tailoring.py — End-to-end tailoring pipeline test.

Usage:
    python tests/test_tailoring.py

Steps:
  1. Load a JD — from test_data/sample_jd.json if it exists; otherwise fetch
     a live JD from the sample email and save it for future runs.
  2. Classify lane (keyword heuristic first, LLM fallback if ambiguous).
  3. Generate tailored resume JSON via Claude.
  4. Generate cover letter JSON via Claude.
  5. Print everything for manual review.
"""

import json
import sys
from pathlib import Path

# Make src/ importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import yaml
from parser.email_parser import parse_alert_from_eml, resolve_sendgrid_url
from scraper.jd_fetcher import fetch_job_description
from classifier.lane_selector import classify_lane
from tailor.resume_tailor import tailor_resume
from tailor.cover_letter import write_cover_letter

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent
EML_PATH     = ROOT / "test_data" / "sample_alert.eml"
SAVED_JD     = ROOT / "test_data" / "sample_jd.json"
CONFIG_PATH  = ROOT / "config" / "settings.yaml"
BANK_PATH    = ROOT / "templates" / "project_bank.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_project_bank() -> list[dict]:
    with open(BANK_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("projects", [])


def get_jd() -> tuple[dict, str]:
    """
    Return (job_dict, jd_text).
    Loads from test_data/sample_jd.json if it exists; otherwise fetches live
    and saves the result.
    """
    if SAVED_JD.exists():
        print(f"Loading saved JD from {SAVED_JD}")
        data = json.loads(SAVED_JD.read_text())
        return data["job"], data["jd_text"]

    print(f"No saved JD found — fetching live from {EML_PATH}")
    jobs = parse_alert_from_eml(EML_PATH, max_jobs=3)
    if not jobs:
        print("ERROR: No jobs parsed from sample email.")
        sys.exit(1)

    # Try each of the first 3 jobs until one yields a good JD
    for job in jobs:
        print(f"  Trying: {job['title']} @ {job['company']}")
        resolved = resolve_sendgrid_url(job["url"])
        if not resolved:
            print("  → Could not resolve URL, trying next")
            continue

        print(f"  → Resolved: {resolved}")
        jd_text = fetch_job_description(resolved, timeout=30, min_length=200)
        if jd_text:
            job["resolved_url"] = resolved
            print(f"  → JD fetched ({len(jd_text):,} chars) — saving to {SAVED_JD}")
            SAVED_JD.write_text(json.dumps({"job": job, "jd_text": jd_text}, indent=2))
            return job, jd_text

        print("  → JD fetch failed, trying next")

    print("ERROR: Could not fetch a JD for any of the sample jobs.")
    sys.exit(1)


def divider(label: str = "") -> None:
    if label:
        pad = (70 - len(label) - 2) // 2
        print(f"\n{'─' * pad} {label} {'─' * pad}")
    else:
        print("\n" + "═" * 70)


def main() -> None:
    config = load_config()
    project_bank = load_project_bank()

    # ── Step 1: Get JD ────────────────────────────────────────────────────────
    divider()
    job, jd_text = get_jd()

    divider("JOB")
    print(f"Title   : {job['title']}")
    print(f"Company : {job['company']}")
    print(f"Location: {job.get('location', 'N/A')}")
    print(f"Salary  : {job.get('salary', 'N/A')}")
    print(f"URL     : {job.get('resolved_url', job.get('url', 'N/A'))}")
    print(f"JD chars: {len(jd_text):,}")

    divider("JD PREVIEW (first 600 chars)")
    print(jd_text[:600])

    # ── Step 2: Classify lane ─────────────────────────────────────────────────
    divider("LANE CLASSIFICATION")
    lane = classify_lane(jd_text=jd_text, lanes_config=config["lanes"])
    print(f"Lane    : {lane['name']} — {lane['label']}")

    # ── Step 3: Tailor resume ─────────────────────────────────────────────────
    divider("RESUME TAILORING")
    print("Calling Claude for resume tailoring…")
    tailored = tailor_resume(
        jd_text=jd_text,
        lane=lane,
        project_bank=project_bank,
        config=config["resume"],
    )

    divider("TAGLINE")
    print(tailored.get("tagline", "(missing)"))

    divider("QUALIFICATION SUMMARY")
    print(tailored.get("summary", "(missing)"))

    divider("KEY SKILLS (9)")
    skills = tailored.get("skills", [])
    for i, s in enumerate(skills, 1):
        print(f"  {i:2d}. {s}")
    if len(skills) != 9:
        print(f"  ⚠ Expected 9 skills, got {len(skills)}")

    divider("ROLE BULLETS")
    role_labels = [
        "Role 0 — Primary Role",
        "Role 1 — Secondary Role",
        "Role 2 — Tertiary Role",
    ]
    for role in tailored.get("roles", []):
        idx = role.get("index", "?")
        label = role_labels[idx] if isinstance(idx, int) and idx < 3 else f"Role {idx}"
        print(f"\n  {label}")
        for b in role.get("bullets", []):
            over = " ⚠ LONG" if len(b) > 180 else ""
            print(f"    • [{len(b):3d}]{over} {b}")

    divider("GAPS NOTED")
    for g in tailored.get("gaps_noted", []):
        print(f"  • {g}")
    if not tailored.get("gaps_noted"):
        print("  (none)")

    divider("KEYWORDS INTEGRATED")
    print("  " + ", ".join(tailored.get("keywords_integrated", [])))

    # ── Step 4: Cover letter ──────────────────────────────────────────────────
    divider("COVER LETTER")
    print("Calling Claude for cover letter…")
    cl = write_cover_letter(
        jd_text=jd_text,
        job=job,
        lane=lane,
        project_bank=project_bank,
        config=config["cover_letter"],
    )

    divider("COVER LETTER TEXT")
    for i, para in enumerate(cl.get("paragraphs", []), 1):
        print(f"[{i}] {para}\n")

    print(f"Projects referenced: {cl.get('projects_referenced', [])}")
    word_count = sum(len(p.split()) for p in cl.get("paragraphs", []))
    print(f"Word count: ~{word_count}")
    if word_count > 300:
        print("  ⚠ Over 300 words — consider trimming")

    # ── Summary ───────────────────────────────────────────────────────────────
    divider("SUMMARY")
    resume_ok = bool(
        tailored.get("tagline")
        and tailored.get("summary")
        and len(tailored.get("skills", [])) == 9
        and tailored.get("roles")
    )
    cl_ok = bool(cl.get("paragraphs"))
    print(f"  Lane        : {lane['name']} — {lane['label']}")
    print(f"  Resume JSON : {'✓ OK' if resume_ok else '✗ INCOMPLETE'}")
    print(f"  Cover letter: {'✓ OK' if cl_ok else '✗ INCOMPLETE'}")
    print()


if __name__ == "__main__":
    main()
