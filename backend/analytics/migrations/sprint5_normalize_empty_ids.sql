-- ============================================================================
-- Sprint 5 — Normalize empty-string academic IDs to NULL.
-- ============================================================================
-- Why?
--   The Users table has UNIQUE constraints on Scholar_ID, Orcid_ID, and
--   Scopus_ID. Postgres treats NULL as "no value" (multiple NULLs are
--   allowed under UNIQUE), but '' is a real value, so multiple users
--   with empty-string IDs collide.
--
--   The Python boundary (accounts.views.approve_registration) now
--   coerces empty → NULL on insert. This migration cleans up the rows
--   that already slipped through with empty strings.
--
-- Idempotent — safe to re-run.
-- ============================================================================

BEGIN;

-- Academic IDs (UNIQUE)
UPDATE "Users" SET "Scholar_ID" = NULL WHERE "Scholar_ID" = '';
UPDATE "Users" SET "Orcid_ID"   = NULL WHERE "Orcid_ID"   = '';
UPDATE "Users" SET "Scopus_ID"  = NULL WHERE "Scopus_ID"  = '';

-- Name columns (FullName_Ar carries a UNIQUE constraint —
-- uq_users_fullname_ar — so the same '' rule applies). The other
-- name columns aren't unique today but cleaning them prevents future
-- surprises if a constraint is added later.
UPDATE "Users" SET "FullName_Ar" = NULL WHERE "FullName_Ar" = '';
UPDATE "Users" SET "FirstName"   = NULL WHERE "FirstName"   = '';
UPDATE "Users" SET "LastName"    = NULL WHERE "LastName"    = '';
UPDATE "Users" SET "MiddleName"  = NULL WHERE "MiddleName"  = '';

COMMIT;
