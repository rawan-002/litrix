-- ============================================================================
-- 20260611_drop_legacy_password.sql   (DESTRUCTIVE — run intentionally)
-- ============================================================================
-- Drops the orphaned plaintext-named "Password" column on Users.
--   * Authentication uses "PasswordHash" — Django's User.password maps there
--     (accounts/models.py: db_column='PasswordHash').
--   * "Password" is mapped to NO Django field and holds 0 rows. It reads as a
--     security smell and confuses new developers.
--   * The only code that touched it (clean_test_emails.py, reset_test_researchers.py)
--     was migrated to "PasswordHash" first.
-- Verified empty before writing this. Irreversible — re-add the column if a
-- rollback is ever needed.
-- Apply with:  python tools/run_migration.py backend/migrations/20260611_drop_legacy_password.sql
-- ============================================================================

ALTER TABLE "Users" DROP COLUMN IF EXISTS "Password";
