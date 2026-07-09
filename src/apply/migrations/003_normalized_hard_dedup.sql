-- src/apply/migrations/003_normalized_hard_dedup.sql
-- H9: switch the HARD UNIQUE index from (company, ats_domain, ats_job_id)
-- to (ats_domain, ats_job_id). Raw company weakens the key; the ATS posting
-- identity is the (ats_domain, ats_job_id) pair. Spelling variance ('Acme'
-- vs 'Acme, Inc.') at the same posting now hits both the was_applied gate
-- AND the UNIQUE constraint.
--
-- NOTE (xhigh-H6): this file is kept for reference — the migration is
-- actually applied programmatically by
-- ``src/apply/dedup.py::_apply_migration_003_gated`` so:
--   (a) the destructive DELETE is gated by the ``schema_migrations`` marker
--       table and can only fire ONCE per DB.
--   (b) the DELETE partition includes ``applicant`` so multi-user rows at
--       the same (ats_domain, ats_job_id) posting survive.
--   (c) both index steps stay idempotent via IF NOT EXISTS / IF EXISTS.
--
-- See .agent/codebase-audit-2026-07-08.md Group C / finding H9 and
-- tests/apply/integration/test_dedup_fail_closed.py::
--   test_h9_hard_dedup_normalized_survives_company_spelling_variance,
--   tests/apply/integration/test_phase2_xhigh_fixes.py::
--   test_migration_003_delete_gated_by_migrations_table,
--   test_migration_003_delete_does_not_clobber_multi_applicant_rows.

-- 1. Applicant-aware de-duplication. Keep the earliest row PER
--    (applicant, ats_domain, ats_job_id) tuple so applicant B's row at the
--    same posting as applicant A's is preserved. Runs ONCE per DB — gated
--    by the ``schema_migrations`` marker in ``_apply_migration_003_gated``.
DELETE FROM applied_jobs
WHERE ats_domain IS NOT NULL
  AND ats_job_id IS NOT NULL
  AND id NOT IN (
      SELECT MIN(id) FROM applied_jobs
      WHERE ats_domain IS NOT NULL AND ats_job_id IS NOT NULL
      GROUP BY applicant, ats_domain, ats_job_id
  );

-- 2. Create the new UNIQUE index. Idempotent via IF NOT EXISTS.
CREATE UNIQUE INDEX IF NOT EXISTS ux_applied_jobs_hard_v2
    ON applied_jobs (ats_domain, ats_job_id);

-- 3. Drop the old (company, ats_domain, ats_job_id) UNIQUE index. Safe on
--    fresh DBs (DROP INDEX IF EXISTS is a no-op).
DROP INDEX IF EXISTS ux_applied_jobs_hard;
