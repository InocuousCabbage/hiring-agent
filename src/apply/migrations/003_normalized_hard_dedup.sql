-- src/apply/migrations/003_normalized_hard_dedup.sql
-- H9: switch the HARD UNIQUE index from (company, ats_domain, ats_job_id)
-- to (ats_domain, ats_job_id). Raw company weakens the key; the ATS posting
-- identity is the (ats_domain, ats_job_id) pair. Spelling variance ('Acme'
-- vs 'Acme, Inc.') at the same posting now hits both the was_applied gate
-- AND the UNIQUE constraint.
--
-- See .agent/codebase-audit-2026-07-08.md Group C / finding H9 and
-- tests/apply/integration/test_dedup_fail_closed.py::
--   test_h9_hard_dedup_normalized_survives_company_spelling_variance.

-- 1. De-duplicate any pre-existing rows on (ats_domain, ats_job_id) so the
--    new UNIQUE index can be created without violation. Keep the earliest row
--    per (ats_domain, ats_job_id) pair. NULL-triples are left untouched —
--    SQLite treats NULL as distinct from NULL under UNIQUE, so job_url-only
--    fallback rows never collide with each other.
DELETE FROM applied_jobs
WHERE ats_domain IS NOT NULL
  AND ats_job_id IS NOT NULL
  AND id NOT IN (
      SELECT MIN(id) FROM applied_jobs
      WHERE ats_domain IS NOT NULL AND ats_job_id IS NOT NULL
      GROUP BY ats_domain, ats_job_id
  );

-- 2. Create the new UNIQUE index. Idempotent via IF NOT EXISTS.
CREATE UNIQUE INDEX IF NOT EXISTS ux_applied_jobs_hard_v2
    ON applied_jobs (ats_domain, ats_job_id);

-- 3. Drop the old (company, ats_domain, ats_job_id) UNIQUE index. Safe on
--    fresh DBs (DROP INDEX IF EXISTS is a no-op).
DROP INDEX IF EXISTS ux_applied_jobs_hard;
