#!/usr/bin/env python3
"""
tests/test_jd_fetcher.py — Integration test: parse sample email + fetch JDs.

Usage:
    cd /path/to/hiring-agent
    python tests/test_jd_fetcher.py

Parses the first 3 jobs from test_data/sample_alert.eml, resolves their
SendGrid URLs, fetches the full job descriptions, and prints a summary.

Output per job:
  - Title, company
  - SendGrid URL (truncated)
  - Resolved hiring.cafe URL
  - Status: OK / FAIL
  - Character count (on success)
  - First 300 characters of the JD (on success)
"""

import sys
from pathlib import Path

# Make src/ importable regardless of how this script is invoked
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser.email_parser import parse_alert_from_eml, resolve_sendgrid_url
from scraper.jd_fetcher import fetch_job_description

EML_PATH = Path(__file__).parent.parent / "test_data" / "sample_alert.eml"
JOBS_TO_TEST = 3


def main() -> None:
    print(f"Email : {EML_PATH}")
    if not EML_PATH.exists():
        print("ERROR: sample_alert.eml not found.")
        sys.exit(1)

    jobs = parse_alert_from_eml(EML_PATH, max_jobs=JOBS_TO_TEST)
    if not jobs:
        print("ERROR: No jobs parsed from sample email.")
        sys.exit(1)

    print(f"Parsed: {len(jobs)} jobs  (testing first {min(JOBS_TO_TEST, len(jobs))})")
    print()

    results = []
    for i, job in enumerate(jobs[:JOBS_TO_TEST], 1):
        print("=" * 70)
        print(f"Job {i}: {job['title']}")
        print(f"  Company : {job['company']}")
        print(f"  SendGrid: {job['url'][:80]}…")

        # Step 1: Resolve SendGrid → hiring.cafe URL
        resolved = resolve_sendgrid_url(job["url"])
        print(f"  Resolved: {resolved or 'FAILED'}")

        if not resolved:
            print("  Status  : FAIL  (could not resolve SendGrid URL)")
            results.append((job["title"], False, 0))
            continue

        # Step 2: Fetch JD via Playwright
        print("  Fetching JD (Playwright)…", flush=True)
        jd = fetch_job_description(resolved, timeout=30, min_length=200)

        if jd:
            print(f"  Status  : OK")
            print(f"  Chars   : {len(jd)}")
            preview = jd[:300].replace("\n", "\n            ")
            print(f"  Preview :\n            {preview}")
            results.append((job["title"], True, len(jd)))
        else:
            print("  Status  : FAIL  (JD too short or no section headers)")
            results.append((job["title"], False, 0))

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    ok = sum(1 for _, success, _ in results if success)
    for title, success, chars in results:
        status = f"OK  ({chars:,} chars)" if success else "FAIL"
        print(f"  {'✓' if success else '✗'}  {title[:50]:50s}  {status}")
    print(f"\n  {ok}/{len(results)} jobs fetched successfully")


if __name__ == "__main__":
    main()
