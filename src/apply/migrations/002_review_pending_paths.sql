-- src/apply/migrations/002_review_pending_paths.sql
-- Phase 1 additive columns for the S12 YES/NO review loop wire-up.
--
-- Findings addressed (see .agent/codebase-audit-2026-07-08.md):
--   H4  — persist resume_path so YES-confirmed re-submits can upload the
--         same tailored resume the review email screenshotted.
--   M1  — persist applicant so `execute_confirmed_submit` loads storage
--         state under the same key bootstrap wrote it (previously the
--         YES branch called `load_state(ats, "")` → miss → unauthenticated
--         re-submit).
--   H4  — persist cover_letter_path alongside resume for symmetry.
--   M12 — persist clarified_at so the AMBIGUOUS branch clarifies once per
--         thread, not once per poll tick (previously up to ~144 duplicate
--         clarification emails over 72h).
--
-- ADD COLUMN is used (not CREATE TABLE) so an existing state DB from
-- migration 001 evolves in place. `IF NOT EXISTS` isn't valid on SQLite's
-- ALTER TABLE ADD COLUMN — the state_store's `_ensure_schema` catches the
-- duplicate-column OperationalError and continues so re-runs are safe.
ALTER TABLE review_pending ADD COLUMN resume_path       TEXT;
ALTER TABLE review_pending ADD COLUMN cover_letter_path TEXT;
ALTER TABLE review_pending ADD COLUMN applicant         TEXT;
ALTER TABLE review_pending ADD COLUMN clarified_at      TEXT;
