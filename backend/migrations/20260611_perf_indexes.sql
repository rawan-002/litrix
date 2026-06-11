-- ============================================================================
-- 20260611_perf_indexes.sql
-- ============================================================================
-- Performance: index the hot foreign-key / join columns. Postgres does NOT
-- auto-index FK columns, and these back the dashboard's most frequent joins
-- and filters (Authors->Paper, Paper->Journal, HoD department scoping, the
-- reports + notifications surfaces). At today's row counts a seq scan is
-- already fast; these keep it fast as the data grows. Non-destructive —
-- IF NOT EXISTS makes the whole file safe to re-run.
-- Apply with:  python tools/run_migration.py backend/migrations/20260611_perf_indexes.sql
-- ============================================================================

CREATE INDEX IF NOT EXISTS ix_authors_paper              ON "Authors" ("PaperID");
CREATE INDEX IF NOT EXISTS ix_researchpaper_journal      ON "ResearchPaper" ("JournalID");
CREATE INDEX IF NOT EXISTS ix_works_in_department        ON "Works_In" ("DepartmentID");
CREATE INDEX IF NOT EXISTS ix_external_authors_paper     ON "ExternalAuthors" ("PaperID");
CREATE INDEX IF NOT EXISTS ix_notification_user          ON "Notification" ("UserID");
CREATE INDEX IF NOT EXISTS ix_syncjob_user               ON "SyncJob" ("UserID");
CREATE INDEX IF NOT EXISTS ix_auditlog_user              ON "AuditLog" ("UserID");
CREATE INDEX IF NOT EXISTS ix_report_submission_user     ON "ReportSubmission" ("UserID");
CREATE INDEX IF NOT EXISTS ix_report_submission_campaign ON "ReportSubmission" ("CampaignID");
CREATE INDEX IF NOT EXISTS ix_report_decision_submission ON "ReportPaperDecision" ("SubmissionID");
