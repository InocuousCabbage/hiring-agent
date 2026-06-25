#!/usr/bin/env python3
"""
tests/test_renderer.py — Render tailored resume + cover letter to DOCX/PDF.

Usage:
    python tests/test_renderer.py

Loads tailored JSON from tests/test_tailoring.py's saved run if available,
or uses a realistic hardcoded sample based on the actual resume structure.
Outputs both files to test_data/output/ and prints paths for visual inspection.
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from pdf_gen.renderer import render_resume_pdf, render_cover_letter_pdf

ROOT       = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "test_data" / "output"
SAVED_JD   = ROOT / "test_data" / "sample_jd.json"

# ── Sample data ───────────────────────────────────────────────────────────────
# Realistic example based on the actual resume + test_tailoring.py output.
# This is used when there's no live tailoring run to pull from.

SAMPLE_JOB = {
    "title": "Lead Product Marketing Manager",
    "company": "Group O",
    "location": "United States (Remote)",
    "salary": "$55–$62.5/hr",
}

SAMPLE_LANE = {
    "name": "pmm",
    "label": "Product Marketing (PMM)",
    "template": "templates/resumes/base_resume.docx",
}

SAMPLE_TAILORED_RESUME = {
    "tagline": "Product & Offer Implementation | Cross-Functional Leadership | Marketing Systems & Analytics",
    "summary": (
        "Marketing professional with 2+ years leading end-to-end implementation of "
        "marketing systems, CRM integrations, and multi-variable offer frameworks across "
        "B2C and B2B portfolios. Proven ability to translate complex business requirements "
        "into technical documentation, manage cross-functional initiatives from concept "
        "through launch, and drive measurable pipeline outcomes through structured "
        "program execution."
    ),
    "skills": [
        "Cross-Functional Program Management",
        "Marketing Requirements Documentation",
        "CRM & System Integration",
        "Product Lifecycle Management",
        "Campaign & Offer Execution",
        "Marketing Attribution Modeling",
        "Agile / Iterative Delivery",
        "Data Analysis & Reporting",
        "A/B Testing & CRO",
    ],
    "roles": [
        {
            "index": 0,
            "bullets": [
                "Led end-to-end implementation of marketing automation platform integrated "
                "with CRM, migrating 100,000+ records and generating "
                "$1M+ in pipeline to date.",
                "Designed a custom CRM integration strategy, architecting field-level data "
                "flow across Contacts, Leads, Opportunities, and Deals, translating complex "
                "business logic into precise technical requirements.",
                "Built and launched a marketing attribution infrastructure defining "
                "first-touch, campaign, referral, and funnel-stage fields across 2 business "
                "lines, enabling cross-channel performance reporting.",
                "Drove 223% average improvement in funnel conversion rates by leading CRO "
                "initiatives including A/B testing, behavioral analysis, and landing page "
                "optimization.",
                "Collaborated across Sales, IT, and Operations teams to implement revenue "
                "attribution models and deliver regular leadership readouts via BI "
                "dashboards.",
            ],
        },
        {
            "index": 1,
            "bullets": [
                "Led a regional product marketing strategy sprint for a new market entry, "
                "defining audience segmentation, channel prioritization, and go-to-market "
                "initiative sequencing.",
                "Advise clients on offer positioning, funnel architecture, and campaign "
                "execution, translating business objectives into actionable marketing and "
                "system requirements.",
                "Audit analytics infrastructure and channel performance to surface "
                "optimization opportunities, delivering structured recommendations aligned "
                "to client KPIs.",
            ],
        },
        {
            "index": 2,
            "bullets": [
                "Design and launch SEO-ready websites with integrated analytics, automation, "
                "and UX improvements; manage ongoing performance, accessibility, and content "
                "updates for multiple clients.",
            ],
        },
    ],
    "gaps_noted": [
        "No direct DIRECTV/telecom billing system experience (C3, STMS, Amdocs); "
        "analogous CRM integration experience with marketing automation and CRM platforms.",
    ],
    "keywords_integrated": [
        "product marketing", "offer implementation", "cross-functional leadership",
        "marketing requirements documentation", "go-to-market", "attribution",
    ],
    "lane": SAMPLE_LANE,
}

SAMPLE_COVER_LETTER = {
    "paragraphs": [
        "Telecom offer implementation is one of the more technically demanding corners of "
        "product marketing — managing hundreds of offer variables, coordinating across "
        "billing, engineering, and GTM teams, and keeping four to six initiatives moving "
        "simultaneously requires the kind of cross-functional discipline that most marketing "
        "roles never develop. That operational depth is where most of my recent work has lived.",

        "At Acme Corp, I led the end-to-end architecture, testing, and launch of a "
        "marketing automation and CRM integration, translating business requirements "
        "into precise technical specs for engineering and systems partners, managing a custom "
        "sync model across leads, opportunities, and project records, and migrating "
        "100,000+ customer records without disrupting the active sales pipeline. The project "
        "generated $1M+ in pipeline to date. That work maps directly to "
        "what this role requires: breaking down marketing and product needs into actionable "
        "technical requirements and managing implementation across multiple systems and "
        "stakeholders.",

        "I also led a marketing attribution infrastructure build in the CRM, scoping field "
        "logic changes, coordinating development across two business lines, and extending "
        "attribution continuity across lead, opportunity, and project records to enable "
        "reporting by channel, campaign, category, and funnel stage.",

        "I'd welcome a conversation about how this experience applies to the DIRECTV product "
        "and offer implementation work at Group O.",
    ],
    "projects_referenced": ["proj_001", "proj_002"],
}


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    # Try to load live job metadata if the JD fetcher saved it
    job = SAMPLE_JOB
    if SAVED_JD.exists():
        try:
            saved = json.loads(SAVED_JD.read_text())
            job = saved.get("job", SAMPLE_JOB)
            print(f"Loaded job metadata from {SAVED_JD}")
        except Exception:
            pass

    today = date.today().isoformat()
    output_dir = OUTPUT_DIR / today

    print(f"\nRendering for: {job['title']} @ {job['company']}")
    print(f"Output dir   : {output_dir}")
    print(f"Date         : {today}")

    # ── Resume ───────────────────────────────────────────────────────────────
    print("\n[1/2] Rendering resume DOCX → PDF...")
    resume_path = render_resume_pdf(
        tailored_resume=SAMPLE_TAILORED_RESUME,
        lane=SAMPLE_LANE,
        job=job,
        date_str=today,
        output_dir=output_dir,
    )
    resume_type = "PDF" if str(resume_path).endswith(".pdf") else "DOCX (no PDF converter)"
    print(f"      {resume_type}: {resume_path}")

    # ── Cover letter ─────────────────────────────────────────────────────────
    print("\n[2/2] Rendering cover letter DOCX → PDF...")
    cl_path = render_cover_letter_pdf(
        cover_letter=SAMPLE_COVER_LETTER,
        job=job,
        date_str=today,
        output_dir=output_dir,
    )
    cl_type = "PDF" if str(cl_path).endswith(".pdf") else "DOCX (no PDF converter)"
    print(f"      {cl_type}: {cl_path}")

    # ── Verify files exist and have content ──────────────────────────────────
    print("\n── File check ──")
    ok = True
    for label, path in [("Resume", resume_path), ("Cover letter", cl_path)]:
        if path.exists():
            size = path.stat().st_size
            status = "✓ OK" if size > 1000 else "⚠ suspiciously small"
            print(f"  {label:15s}: {size:>8,} bytes  {status}  {path.name}")
        else:
            print(f"  {label:15s}: ✗ NOT FOUND at {path}")
            ok = False

    if ok:
        print("\nOpen to inspect:")
        print(f"  open '{resume_path}'")
        print(f"  open '{cl_path}'")
    else:
        print("\n⚠ One or more files missing — check logs above.")


if __name__ == "__main__":
    main()
