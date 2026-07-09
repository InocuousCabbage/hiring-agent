# Auto-apply flow (Phase 3 MVP)

Operator manual for the auto-apply pipeline. MVP surface only: Greenhouse
only, review-mode default, dry-run held closed until the checks in
`## Success criteria for enabling dry_run: false` are green.

## Overview

Auto-apply is an opt-in pipeline stage. When a job URL matches an
adapter in `apply.allowed_ats`, the dispatcher opens the ATS form with a
stored login session, fills from `templates/candidate_profile.yaml`,
screenshots the pre-submit page, and stages a Gmail review email. The
operator replies YES or NO on the first line; only YES re-opens the
browser and submits. Duplicates, rate limits, and CAPTCHAs short-circuit
before submit; every terminal state is recorded in `state/applied_jobs.db`.

## Prerequisites

- Complete base setup in [SETUP.md](../SETUP.md) through Gmail OAuth.
- Chromium via `python -m playwright install chromium`.
- `apply.enabled: true` in `config/settings.yaml`; `apply.dry_run` stays
  `true` until success criteria pass.
- `templates/candidate_profile.yaml`, schema in
  `templates/candidate_profile.yaml.example`.

## Bootstrap

Runs once per ATS. `python -m src.apply.bootstrap greenhouse` opens a
headed Chromium, waits on `page.wait_for_url` with a 5-min timeout for
MFA, snapshots the session, and writes storage state to the OS keyring
under service `hiring-agent.<ats>.<user>` (fallback: Fernet-encrypted
file under `config/credentials/apply/` at directory mode `0o700` and
file mode `0o600`). Check without re-login via
`python -m src.apply.bootstrap --status`. Re-run whenever the digest
says `Bootstrap needed`.

## Configuration

Keys under `apply:` in `config/settings.yaml`; defaults are safe (master
switch off, review mode, dry-run on, Greenhouse only).

The schema is frozen: `_validate_apply_config` in `src/main.py` requires
**all 21 top-level keys plus the 4 keys under `browserbase`** when
`apply.enabled: true`. There are no per-key defaults for missing keys —
omitting any one raises `ConfigError: apply: missing required key: <name>`
at startup. The shipped `config/settings.yaml` already contains the full
block; copy it verbatim if you regenerate.

```yaml
apply:
  enabled: false                       # master switch; default OFF (safety)
  mode: review                         # review | auto (auto off in MVP)
  allowed_ats: [greenhouse]            # Phase 3 MVP; 3.5 adds lever, ashby
  long_tail: none                      # none | computer_use
  dry_run: true                        # fill + screenshot, never click submit
  timeout_seconds: 90
  navigation_retries: 2
  rate_limit_per_ats_per_day: 10       # int in (0, 100]
  review_timeout_hours: 72
  review_reping_hours: 24              # must be < review_timeout_hours
  retention_days: 30
  screenshot_dir: state/screenshots
  trace_dir: state/traces
  storage_state_dir: config/credentials/apply
  dedup_db_path: state/applied_jobs.db
  captcha_action: escalate             # escalate | skip
  captcha_transport: browserbase       # browserbase | local
  profile_path: templates/candidate_profile.yaml
  gmail_label_prefix: "hiring-agent/apply"
  fast_path_recipient: env:MY_EMAIL    # env:<VAR> or literal address
  browserbase:                         # all 4 sub-keys required
    enabled: true
    solve_captchas: true
    proxies: true
    block_ads: true
```

## Pipeline flow

Per job URL that survives the classifier and tailor stages:

1. Dispatcher matches URL against `apply.allowed_ats`; unmatched skip.
2. Dedup DB checks hard key `(company, ats_domain, ats_job_id)`.
3. Rate limiter checks today's count for this ATS.
4. Browser session opens (local or Browserbase per config).
5. CAPTCHA detector scans the DOM before form interaction.
6. Adapter fills the form and screenshots; submit stays unclicked.
7. Review email is staged under `hiring-agent/apply/pending`.
8. On operator YES, `execute_confirmed_submit` re-opens the browser and
   records the result.

## Review mode (default)

The shipped default and the only mode that reaches submit in Phase 3:

- Nested Gmail labels created on boot: `hiring-agent/apply/pending`,
  `hiring-agent/apply/submitted`, `hiring-agent/apply/declined`.
- Parser reads the first non-quoted line, splits on whitespace, compares
  the first token case-insensitively against `YES` and `NO`. Anything
  else auto-replies `please reply YES or NO on the first line`.
- 24 hours after first send, the poller re-pings with a reminder that
  auto-decline fires at 72 hours. 72 hours after first send, the row
  auto-declines: label moves to `hiring-agent/apply/declined`,
  `apply.review.auto_declined` fires, the row appears in the next digest.
- Soft-dup override: when the second listing hit only the soft index,
  the digest offers `Reply YES to override`; that YES bypasses the soft
  warning for that single job.

## Dedup semantics

Two indices back `state/applied_jobs.db`:

- Hard: `UNIQUE(company, ats_domain, ats_job_id) ON CONFLICT ABORT`. A
  second insert with the same tuple raises; pipeline returns
  `already_applied` without opening a browser.
- Soft: index on `(company_normalized, role_title_normalized)`.
  Normalization lowercases, strips punctuation, strips legal suffixes
  matching `Inc|LLC|Corp|Ltd|GmbH|Co|Company`, strips seniority prefixes
  matching `Sr|Senior|Jr|Junior|Staff|Principal|Lead`. A soft match
  routes to review with a warning instead of hard-blocking.
- Escape hatch: `python -m src.apply.dedup --unblock <job_url>` deletes
  the row and prints the affected count.

## Rate limiting

`apply.rate_limit_per_ats_per_day` defaults to `10`. The 11th submission
for an ATS on a UTC day returns `status="rate_limited"`, logs
`apply.rate_limited`, and shows in the next digest under
`Rate-limited — will retry tomorrow`. The counter resets on the UTC day
boundary; no manual reset.

## CAPTCHA handling

Detection is DOM-marker based, never vision. Five kinds are recognized:
Cloudflare Turnstile, reCAPTCHA v2, reCAPTCHA v3, hCaptcha, DataDome.

- `apply.captcha_transport: browserbase` routes the challenged
  navigation through Browserbase with `solve_captchas=true` and
  `proxies=true`. Browserbase's `replay_url` is captured into
  `ApplyResult.human_review_url`.
- `apply.captcha_transport: local` short-circuits: no local solve, row
  marked `captcha_escalated`, fast-path email with subject prefix
  `[hiring-agent] URGENT:` fires to `MY_EMAIL`.
- Phase 3.6 will spike Turnstile solve rate through Browserbase before
  Workday and iCIMS adapters land.

## Computer Use fallback (opt-in)

`apply.long_tail: computer_use` (default `none`) enables the Claude
Computer Use adapter as fallback for URLs no deterministic adapter
recognizes. This adapter is **HARD-CODED to review_required** regardless
of `apply.mode`. No config change inside Phase 3 can make it auto-submit.
File uploads short-circuit out of the LLM: any `<input type="file">` is
handed to Playwright's `set_input_files` directly.

Warning: LLM-driven form-fill has a documented eligibility hallucination
risk. The adapter may accept a job it should not qualify for and answer
authorization or experience questions in ways that misrepresent the
applicant. Review every staged application before YES.

## Retention

`apply.retention_days` defaults to `30`. At the end of `run_pipeline`,
the retention job deletes files older than that from `state/traces/` and
`state/screenshots/` and logs the removed count. The dedup DB is never
rotated — it is the historical record.

## Logging + PII

A structlog processor installed at boot redacts any key-value pair whose
key matches, case-insensitively,
`email|phone|first_name|last_name|address|linkedin|answer|prompt|raw|value`.
Adapters log event names, not field contents. Standard events:
`apply.form_navigated`, `apply.form_filled`, `apply.captcha_detected`,
`apply.submitted`, `apply.review_required`, `apply.failed`,
`apply.dedup_hit`, `apply.rate_limited`, `apply.review.auto_declined`.

## Live testing

Live tests are opt-in and never run in CI:
`HIRING_AGENT_LIVE_ATS=1 pytest -m live_ats tests/apply/live/`. First
live target is `boards.greenhouse.io/greenhouse` — Greenhouse's own demo
board. Live tests never target a real employer and always run with
`apply.dry_run: true`.

## Success criteria for enabling dry_run: false

All six pass before flipping `apply.dry_run` to `false`.

1. `pytest tests/apply/` — all offline tests green.
2. `pytest -m live_ats tests/apply/live/test_greenhouse_demo.py` against
   `boards.greenhouse.io/greenhouse` succeeds in dry-run: form fills,
   screenshot saves, submit not clicked,
   `apply.dry_run.holding_at_submit` logs.
3. `python -m src.apply.bootstrap greenhouse` completes with a real
   login and sets `0o700` / `0o600` on the credentials directory.
4. `python -m src.apply.bootstrap --status` reports
   `greenhouse: bootstrapped, last_verified=<iso>`.
5. Full `pytest tests/` — pre-existing offline suite still passes.
6. 7-day soak on the demo board with dry-run on: one cron tick per day,
   full review loop exercised, zero unexpected errors, zero orphan
   `review_pending` rows past 72 hours, zero double-inserts.

## Out of scope

Locked out of Phase 3 and 3.5:

- LinkedIn Easy Apply.
- The `_ALLOWED_COMPANIES` placeholder — kept as-is by design.
- Verified Browser / Cloudflare Signed Agents wiring — Q_BB2 locked on
  the solveCaptchas API only.
- No multi-user support beyond the keyring service-name pattern.
- Full-auto default (`apply.mode: auto` is not shipped default).
- No post-submit lifecycle (screener replies, scheduling, offers).
- No dashboard over `state/applied_jobs.db`.
- No salary negotiation or offer parsing.
- Cross-ATS resume-of-record.
- cortextOS Telegram push for review cards.

## Troubleshooting

### Session expired for a given ATS

Digest says `Bootstrap needed — <ats> session expired`, or the log
emits `apply.session_expired`. Re-run
`python -m src.apply.bootstrap <ats>` and confirm with `--status`.

### Review email never arrived

Check the `hiring-agent/apply/pending` label — the row is likely staged
but the send failed. Look for `apply.review_required` then
`apply.email.send_failed` in the log. The scrubber redacts values, it
does not drop rows.

### Duplicate blocked but you want to apply anyway

For a soft-dup, reply YES on the digest thread that offered the
override. For a hard-dup, run
`python -m src.apply.dedup --unblock <job_url>` and re-run.

### Unexpected CAPTCHA on a page that should not have one

Usually a new DOM marker — check the S9 fixtures. Confirm
`apply.captcha_transport` and Browserbase quota. Fall back to `local`
transport temporarily to force fast-path escalation while you fix the
detector.
