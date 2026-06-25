"""
main.py — Orchestrator for the Hiring.cafe job alert agent.

Runs the full pipeline:
  Gmail intake → Parse → Fetch JDs → Classify → Tailor → QA → PDF → Digest

Flags:
  --test      Load from test_data/sample_alert.eml instead of Gmail.
              Implies dry-run (no email send, no mark-processed).
  --dry-run   Run the full pipeline but skip sending digest and marking processed.
"""

import argparse
import os
import sys
import yaml
import structlog
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from parser.email_parser import parse_alert_from_eml, parse_alert_email
from scraper.jd_fetcher import fetch_job_description
from classifier.lane_selector import classify_lane
from tailor.resume_tailor import tailor_resume
from tailor.cover_letter import write_cover_letter
from qa.checker import run_qa, auto_fix
from pdf_gen.renderer import render_resume, render_cover_letter
from gmail.digest import compose_digest
from contacts.hm_finder import find_hiring_manager

log = structlog.get_logger()


def load_config() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def load_project_bank() -> list[dict]:
    with open(ROOT / "templates" / "project_bank.yaml") as f:
        data = yaml.safe_load(f)
    return data.get("projects", [])


def _build_attachments(processed: list[dict]) -> list[Path]:
    """Flatten per-job (resume_pdf, resume_docx, cover_letter_pdf,
    cover_letter_docx) into a single deduped attachment list.

    Resilient to:
      - PDF fallback: renderer returns None in the pdf slot when no
        LibreOffice/docx2pdf is installed — those Nones are filtered out, so
        only real files are attached and the digest body can honestly say
        "DOCX only" instead of falsely claiming a PDF + DOCX pair exists.
      - Partial-rollout dicts: missing keys (e.g. 'resume_docx' from a stale
        producer) are tolerated via .get() instead of raising KeyError.
      - Duplicate paths (e.g. older callers that returned the same docx in
        both pdf and docx slots) are deduped so attachments don't double up.
    """
    keys = (
        "resume_pdf",
        "resume_docx",
        "cover_letter_pdf",
        "cover_letter_docx",
    )
    attachments: list[Path] = []
    seen: set = set()
    for p in processed:
        for key in keys:
            path = p.get(key)
            if path is None:
                continue
            if path not in seen:
                seen.add(path)
                attachments.append(path)
    return attachments


def run_pipeline(
    jobs: list[dict],
    config: dict,
    project_bank: list[dict],
    today: str,
    output_dir: Path,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Process a list of parsed job dicts through the full pipeline.
    Returns (processed, skipped).
    """
    processed = []
    skipped = []

    for i, job in enumerate(jobs):
        job_log = log.bind(job_index=i, title=job["title"], company=job["company"])
        job_log.info("step.process_job", status="starting")

        try:
            # ── Fetch JD ─────────────────────────────────────────────────────
            jd = fetch_job_description(
                url=job["url"],
                timeout=config["scraper"]["timeout_seconds"],
                min_length=config["scraper"]["min_jd_length"],
                job_title=job.get("title", ""),
                company=job.get("company", ""),
            )
            if jd is None:
                job_log.warning("step.fetch_jd", status="skipped", reason="JD retrieval failed")
                skipped.append({**job, "reason": "JD retrieval failed"})
                continue
            job_log.info("step.fetch_jd", status="success", jd_length=len(jd))

            # ── Classify lane ─────────────────────────────────────────────────
            lane = classify_lane(jd_text=jd, lanes_config=config["lanes"])
            job_log.info("step.classify_lane", lane=lane["name"])

            # ── Tailor resume ─────────────────────────────────────────────────
            tailored_resume = tailor_resume(
                jd_text=jd,
                lane=lane,
                project_bank=project_bank,
                config=config["resume"],
            )

            confidence = tailored_resume.get("confidence_score", 100)
            min_confidence = config["resume"].get("min_confidence_score", 30)
            if confidence < min_confidence:
                job_log.warning(
                    "step.tailor_resume",
                    status="skipped_low_confidence",
                    confidence=confidence,
                    threshold=min_confidence,
                )
                skipped.append({
                    **job,
                    "reason": f"Poor fit — confidence {confidence}/100 (threshold {min_confidence})",
                })
                continue

            # ── Write cover letter ────────────────────────────────────────────
            cover_letter = write_cover_letter(
                jd_text=jd,
                job=job,
                lane=lane,
                project_bank=project_bank,
                config=config["cover_letter"],
            )

            # ── QA + auto-fix loop ────────────────────────────────────────────
            qa_passed = False
            for attempt in range(config["qa"]["max_retries"] + 1):
                qa_result = run_qa(
                    tailored_resume=tailored_resume,
                    cover_letter=cover_letter,
                    jd_text=jd,
                    lane=lane,
                    config=config,
                )
                if qa_result["pass"]:
                    qa_passed = True
                    break

                job_log.warning(
                    "step.qa",
                    attempt=attempt + 1,
                    errors=qa_result["errors"],
                )

                if attempt < config["qa"]["max_retries"]:
                    tailored_resume, cover_letter = auto_fix(
                        tailored_resume=tailored_resume,
                        cover_letter=cover_letter,
                        issues=qa_result["errors"],
                        jd_text=jd,
                        lane=lane,
                        project_bank=project_bank,
                    )

            if not qa_passed:
                job_log.error("step.qa", status="failed_after_retries")
                skipped.append({**job, "reason": "QA failed after retries"})
                continue

            # ── Render DOCX + PDF ─────────────────────────────────────────────
            # DOCX is always produced; PDF is Optional (None when no
            # LibreOffice/docx2pdf is installed). Downstream code (digest body
            # + attachments) handles the None case explicitly.
            output_dir.mkdir(parents=True, exist_ok=True)
            resume_pdf, resume_docx = render_resume(
                tailored_resume=tailored_resume,
                lane=lane,
                job=job,
                date_str=today,
                output_dir=output_dir,
            )
            cl_pdf, cl_docx = render_cover_letter(
                cover_letter=cover_letter,
                job=job,
                date_str=today,
                output_dir=output_dir,
            )
            job_log.info(
                "step.render_documents",
                resume_pdf=str(resume_pdf) if resume_pdf else None,
                resume_docx=str(resume_docx),
                cover_letter_pdf=str(cl_pdf) if cl_pdf else None,
                cover_letter_docx=str(cl_docx),
            )

            # ── Hiring manager lookup ──────────────────────────────────────
            hm_info = None
            contacts_config = config.get("contacts", {})
            if contacts_config.get("enabled", False):
                try:
                    hm_info = find_hiring_manager(
                        job=job,
                        jd_text=jd,
                        lane=lane["label"],
                        config=contacts_config,
                    )
                    if hm_info:
                        job_log.info("step.hm_lookup", name=hm_info["name"],
                                     confidence=hm_info["confidence"])
                    else:
                        job_log.info("step.hm_lookup", status="not_found")
                except Exception as exc:
                    job_log.warning("step.hm_lookup", status="error", error=str(exc))

            processed.append({
                **job,
                "lane": lane["label"],
                "resume_pdf": resume_pdf,
                "resume_docx": resume_docx,
                "cover_letter_pdf": cl_pdf,
                "cover_letter_docx": cl_docx,
                "hiring_manager": hm_info,
            })

        except Exception as exc:
            job_log.error("step.process_job", status="error", error=str(exc), exc_info=True)
            skipped.append({**job, "reason": f"Unexpected error: {exc}"})

    return processed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Hiring.cafe job alert agent")
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Load from test_data/sample_alert.eml instead of Gmail. "
            "Skips Gmail auth, digest send, and mark-processed."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip sending digest and marking email processed.",
    )
    args = parser.parse_args()

    config = load_config()
    project_bank = load_project_bank()
    today = date.today().isoformat()

    # ── Test mode ─────────────────────────────────────────────────────────────
    if args.test:
        eml_path = ROOT / "test_data" / "sample_alert.eml"
        output_dir = ROOT / "test_data" / "output" / today

        log.info("pipeline.test_mode", eml=str(eml_path), output_dir=str(output_dir))
        jobs = parse_alert_from_eml(eml_path, max_jobs=config["jobs"]["max_per_run"])
        if not jobs:
            log.error("pipeline.test_mode", status="no_jobs_parsed")
            sys.exit(1)
        log.info("pipeline.test_mode", job_count=len(jobs))

        processed, skipped = run_pipeline(
            jobs=jobs,
            config=config,
            project_bank=project_bank,
            today=today,
            output_dir=output_dir,
            dry_run=True,
        )

        print(f"\n{'='*60}")
        print(f"TEST MODE COMPLETE  ({today})")
        print(f"{'='*60}")
        print(f"Processed : {len(processed)}")
        for p in processed:
            print(f"\n  • {p['title']} @ {p['company']}  [{p['lane']}]")

            # Resume: PDF is Optional. None → fallback mode (no PDF converter)
            if p["resume_pdf"] is None:
                print(f"    Resume (DOCX, no PDF converter) : {p['resume_docx']}")
            else:
                print(f"    Resume PDF                       : {p['resume_pdf']}")
                print(f"    Resume DOCX                      : {p['resume_docx']}")

            # Cover letter: same Optional-pdf handling
            if p["cover_letter_pdf"] is None:
                print(f"    Cover Letter (DOCX, no PDF converter) : {p['cover_letter_docx']}")
            else:
                print(f"    Cover Letter PDF                       : {p['cover_letter_pdf']}")
                print(f"    Cover Letter DOCX                      : {p['cover_letter_docx']}")
            hm = p.get("hiring_manager")
            if hm:
                print(f"    Hiring Manager       : {hm['name']} — {hm.get('title', 'N/A')} ({hm['confidence']})")
                if hm.get("linkedin_url"):
                    print(f"    LinkedIn             : {hm['linkedin_url']}")
                if hm.get("outreach_note"):
                    print(f"    Outreach Note        : {hm['outreach_note']}")
        print(f"\nSkipped   : {len(skipped)}")
        for s in skipped:
            print(f"  • {s['title']} @ {s['company']}  — {s['reason']}")
        print()
        return

    # ── Production / dry-run mode ─────────────────────────────────────────────
    from gmail.client import GmailClient

    gmail = GmailClient()
    log.info("step.gmail_intake", status="starting")

    alert = gmail.find_unprocessed_alert(
        sender=config["gmail"]["alert_sender"],
        subject_contains=config["gmail"]["alert_subject_contains"],
        processed_label=config["gmail"]["processed_label"],
    )

    if alert is None:
        log.info("step.gmail_intake", status="no_new_alerts")
        return

    log.info("step.gmail_intake", status="found_alert", message_id=alert["id"])

    jobs = parse_alert_email(
        html_body=alert["html"],
        text_body=alert.get("text", ""),
        max_jobs=config["jobs"]["max_per_run"],
    )
    log.info("step.parse_jobs", job_count=len(jobs))

    if not jobs:
        log.warning("step.parse_jobs", status="no_jobs_found")
        if not args.dry_run:
            gmail.mark_processed(alert["id"], config["gmail"]["processed_label"])
        return

    output_dir = ROOT / "output" / today
    processed, skipped = run_pipeline(
        jobs=jobs,
        config=config,
        project_bank=project_bank,
        today=today,
        output_dir=output_dir,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        recipient = os.getenv("MY_EMAIL")
        if not recipient:
            log.error("step.send_digest", status="aborted", reason="MY_EMAIL not set")
        else:
            subject = config["gmail"]["digest_subject_template"].format(date=today)
            attachments = _build_attachments(processed)
            body = compose_digest(
                processed=processed,
                skipped=skipped,
                attachments=attachments,
            )
            try:
                gmail.send_digest(
                    to=recipient,
                    subject=subject,
                    body_text=body,
                    attachments=attachments,
                )
                log.info("step.send_digest", status="sent", to=recipient)
                gmail.mark_processed(alert["id"], config["gmail"]["processed_label"])
            except Exception as exc:
                log.error("step.send_digest", status="failed", error=str(exc))

    log.info(
        "pipeline.complete",
        processed=len(processed),
        skipped=len(skipped),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
