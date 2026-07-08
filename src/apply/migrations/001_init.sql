-- src/apply/migrations/001_init.sql
-- S5 dedup DB schema — frozen source-of-truth for master-plan §4.6.
-- Consumed by S8 (adapters), S12 (review loop), S14 (digest), S17 (seam).
--
-- Two tables:
--   applied_jobs   — one row per apply attempt worth remembering (see record()
--                    status gating in dedup.py). HARD dedup index on the
--                    (company, ats_domain, ats_job_id) triple. SOFT-warning
--                    lookup index on the normalized (company, role) pair.
--   review_pending — one row per apply that needed human review; the Gmail
--                    review loop writes into this and updates the resolution.

CREATE TABLE IF NOT EXISTS applied_jobs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant                 TEXT    NOT NULL,
    company                   TEXT    NOT NULL,
    company_normalized        TEXT    NOT NULL,
    role_title                TEXT    NOT NULL,
    role_title_normalized     TEXT    NOT NULL,
    ats                       TEXT,                     -- adapter key, e.g. "greenhouse"
    ats_domain                TEXT,                     -- host of apply_url, e.g. "boards.greenhouse.io"
    ats_job_id                TEXT,                     -- provider job id extracted from apply_url
    job_url                   TEXT    NOT NULL,
    apply_url                 TEXT,
    application_id            TEXT,
    status                    TEXT    NOT NULL,         -- ApplyResult.status
    review_id                 TEXT,                     -- populated iff result.review_id is not None
    confirmation_screenshot   TEXT,
    trace_path                TEXT,
    applied_at                TEXT    NOT NULL,         -- ISO-8601 UTC with +00:00 suffix
    submitted_at              TEXT
);

-- HARD dedup: SQLite treats NULL as distinct from NULL under UNIQUE, which is
-- what we want — a missing ats_job_id must NOT collide with another missing
-- one. The job_url fallback in was_applied() handles the None-triple case.
CREATE UNIQUE INDEX IF NOT EXISTS ux_applied_jobs_hard
    ON applied_jobs (company, ats_domain, ats_job_id);

-- SOFT dedup surface: fast lookup by normalized (company, role) pair.
CREATE INDEX IF NOT EXISTS ix_applied_jobs_soft
    ON applied_jobs (company_normalized, role_title_normalized);

-- Rate-limit surface: count rows per ATS domain per day.
CREATE INDEX IF NOT EXISTS ix_applied_jobs_ats_day
    ON applied_jobs (ats_domain, applied_at);


-- H1 fix: this schema is the SINGLE SOURCE OF TRUTH for review_pending.
-- state_store.py's CRUD writes against these column names. The prior
-- (12-column) shape drifted from state_store's (15-column) schema and
-- caused `no such column: first_sent_at` errors on first prod insert.
-- See tests/apply/test_h1_schema_reconciliation.py.
CREATE TABLE IF NOT EXISTS review_pending (
    review_id           TEXT PRIMARY KEY,
    job_url             TEXT NOT NULL,
    apply_url           TEXT NOT NULL,
    company             TEXT NOT NULL,
    role_title          TEXT NOT NULL,
    ats                 TEXT NOT NULL,
    filled_at           TEXT NOT NULL,      -- when the form was filled (pre-review)
    screenshot_path     TEXT NOT NULL,
    trace_path          TEXT,
    first_sent_at       TEXT NOT NULL,      -- when the review email was first sent
    last_repinged_at    TEXT,               -- when we last re-pinged the reviewer
    repings_sent        INTEGER NOT NULL DEFAULT 0,
    gmail_thread_id     TEXT,
    resolution          TEXT,               -- e.g. 'submitted', 'declined', 'auto_declined'
    resolved_at         TEXT
);
