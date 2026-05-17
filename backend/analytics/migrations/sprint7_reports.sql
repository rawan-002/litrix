-- ============================================================================
-- Sprint 7: Reporting Campaigns + Scheduled Notifications
-- ============================================================================
-- Domain: admins open a "verification window" for a target set of years.
-- Researchers see their auto-populated paper list during the window and
-- mark each paper as confirmed / not_mine, plus report any missing
-- papers. The admin sees aggregated decisions and a "missing papers"
-- inbox to ingest into the catalog.
--
-- Tables added:
--   ReportCampaign        — the admin-opened window
--   ReportSubmission      — one row per (campaign × researcher)
--   ReportPaperDecision   — per-paper verdict inside a submission
--   ScheduledNotification — future-send notification queue (admin composer)
--
-- Design notes:
--   • All status columns use a CHECK constraint instead of an ENUM type,
--     so we can add new states later without a DDL dance.
--   • Audit-friendly FKs: ON DELETE rules NEVER cascade onto Users
--     (SET NULL preserves history). Submission → Campaign DOES cascade
--     because deleting a campaign should remove its submissions.
--   • ScheduledNotification stays separate from the existing
--     Notification table — that one is the delivered inbox; this one
--     is the outbound queue.
--
-- Idempotent: uses IF NOT EXISTS + ON CONFLICT throughout so re-running
-- this script is a no-op.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ReportCampaign — the window an admin opens
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "ReportCampaign" (
    "CampaignID"        SERIAL          PRIMARY KEY,
    "TenantID"          INTEGER         NOT NULL DEFAULT 1,
    "Title"             VARCHAR(200)    NOT NULL,
    "Description"       TEXT,

    -- Years the campaign asks about (e.g. {2025, 2026}).
    -- Stored as an int[] so we can use ANY() in the submission generator.
    "TargetYears"       INTEGER[]       NOT NULL,

    "OpensAt"           TIMESTAMPTZ     NOT NULL,
    "ClosesAt"          TIMESTAMPTZ     NOT NULL,

    -- draft   → being composed, not visible to researchers
    -- active  → window is open, submissions accepted
    -- closed  → window passed, read-only
    -- archived→ hidden from default lists (kept for history)
    "Status"            VARCHAR(20)     NOT NULL DEFAULT 'draft',

    -- all       → every Researcher in the tenant
    -- department→ ScopeFilter.department_id (or array of ids)
    -- custom    → ScopeFilter.user_ids[]
    "ScopeType"         VARCHAR(20)     NOT NULL DEFAULT 'all',
    "ScopeFilter"       JSONB,

    "CreatedByUserID"   INTEGER         REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "CreatedAt"         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    "ClosedAt"          TIMESTAMPTZ,
    "ArchivedAt"        TIMESTAMPTZ,

    CONSTRAINT chk_campaign_status
        CHECK ("Status" IN ('draft', 'active', 'closed', 'archived')),
    CONSTRAINT chk_campaign_scope
        CHECK ("ScopeType" IN ('all', 'department', 'custom')),
    CONSTRAINT chk_campaign_dates
        CHECK ("ClosesAt" > "OpensAt"),
    CONSTRAINT chk_target_years_nonempty
        CHECK (array_length("TargetYears", 1) >= 1)
);

CREATE INDEX IF NOT EXISTS idx_campaign_tenant_status
    ON "ReportCampaign" ("TenantID", "Status");

-- Hot path for the "auto-close" worker — quickly find active campaigns
-- whose ClosesAt has passed.
CREATE INDEX IF NOT EXISTS idx_campaign_active_closing
    ON "ReportCampaign" ("ClosesAt")
    WHERE "Status" = 'active';


-- ---------------------------------------------------------------------------
-- 2. ReportSubmission — one row per (campaign × researcher)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "ReportSubmission" (
    "SubmissionID"      SERIAL          PRIMARY KEY,
    "CampaignID"        INTEGER         NOT NULL
                            REFERENCES "ReportCampaign"("CampaignID") ON DELETE CASCADE,
    "UserID"            INTEGER         NOT NULL
                            REFERENCES "Users"("UserID") ON DELETE CASCADE,

    -- pending   → row exists, researcher hasn't opened it
    -- in_progress→ researcher started clicking decisions
    -- submitted → researcher pressed Submit (read-only thereafter)
    -- reopened  → admin reopened for revision (back to writeable)
    "Status"            VARCHAR(20)     NOT NULL DEFAULT 'pending',

    "StartedAt"         TIMESTAMPTZ,
    "SubmittedAt"       TIMESTAMPTZ,
    "ReopenedAt"        TIMESTAMPTZ,
    "ReopenedByUserID"  INTEGER         REFERENCES "Users"("UserID") ON DELETE SET NULL,

    -- Flagged when SubmittedAt > Campaign.ClosesAt. Set by the submit
    -- endpoint, not by a generated column, because admins may override.
    "IsLate"            BOOLEAN         NOT NULL DEFAULT FALSE,

    "AdminReviewedAt"   TIMESTAMPTZ,

    CONSTRAINT chk_submission_status
        CHECK ("Status" IN ('pending', 'in_progress', 'submitted', 'reopened')),

    -- One submission per researcher per campaign — period.
    CONSTRAINT uq_submission_campaign_user
        UNIQUE ("CampaignID", "UserID")
);

CREATE INDEX IF NOT EXISTS idx_submission_user_status
    ON "ReportSubmission" ("UserID", "Status");

CREATE INDEX IF NOT EXISTS idx_submission_campaign
    ON "ReportSubmission" ("CampaignID");


-- ---------------------------------------------------------------------------
-- 3. ReportPaperDecision — researcher's verdict per paper
-- ---------------------------------------------------------------------------
-- A submission contains many decisions. For existing papers the
-- researcher clicks Confirm / Not mine. For papers that are absent
-- from the auto-populated list, the researcher fills a small form
-- (title + year, DOI optional); those rows have PaperID=NULL until an
-- admin resolves them into the catalog.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "ReportPaperDecision" (
    "DecisionID"        SERIAL          PRIMARY KEY,
    "SubmissionID"      INTEGER         NOT NULL
                            REFERENCES "ReportSubmission"("SubmissionID") ON DELETE CASCADE,
    "PaperID"           INTEGER         REFERENCES "ResearchPaper"("PaperID") ON DELETE SET NULL,

    -- confirmed → "this paper is mine and correctly attributed"
    -- not_mine  → "Scholar misattributed this — delete the Authors row"
    -- missing   → "I have a paper that isn't in your list — here it is"
    "Decision"          VARCHAR(20)     NOT NULL,
    "Note"              TEXT,
    "DecidedAt"         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- For Decision='missing': researcher-supplied metadata. Admin resolves
    -- this to a real PaperID (existing or newly scraped/inserted) and
    -- stamps MissingResolvedAt + MissingResolvedToPaperID.
    "MissingTitle"      TEXT,
    "MissingDOI"        TEXT,
    "MissingYear"       INTEGER,
    "MissingResolvedAt"          TIMESTAMPTZ,
    "MissingResolvedToPaperID"   INTEGER REFERENCES "ResearchPaper"("PaperID") ON DELETE SET NULL,

    CONSTRAINT chk_decision_value
        CHECK ("Decision" IN ('confirmed', 'not_mine', 'missing')),

    -- confirmed/not_mine MUST reference an existing PaperID.
    -- missing MUST carry at least a title and a year.
    CONSTRAINT chk_decision_shape CHECK (
        ("Decision" IN ('confirmed', 'not_mine') AND "PaperID" IS NOT NULL)
        OR
        ("Decision" = 'missing'
            AND "MissingTitle" IS NOT NULL
            AND "MissingYear"  IS NOT NULL)
    ),

    -- Prevent duplicate decisions on the same paper inside one submission.
    -- (PaperID can be NULL for missing-entries; Postgres allows multiple
    -- NULLs in UNIQUE, which is what we want — many missing entries OK.)
    CONSTRAINT uq_decision_submission_paper
        UNIQUE ("SubmissionID", "PaperID")
);

CREATE INDEX IF NOT EXISTS idx_decision_submission
    ON "ReportPaperDecision" ("SubmissionID");

-- Fast path for the admin "missing-paper inbox" — pending entries only.
CREATE INDEX IF NOT EXISTS idx_decision_missing_pending
    ON "ReportPaperDecision" ("DecidedAt")
    WHERE "Decision" = 'missing' AND "MissingResolvedAt" IS NULL;


-- ---------------------------------------------------------------------------
-- 4. ScheduledNotification — outbound notification queue
-- ---------------------------------------------------------------------------
-- The existing "Notification" table is the delivered inbox (one row per
-- (user, message)). This new table is the SOURCE — admin composes a
-- single ScheduledNotification with a target-audience spec and a
-- send_at time; a periodic worker (`python manage.py send_due_notifications`)
-- expands it into per-user Notification rows when SendAt is reached.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "ScheduledNotification" (
    "ScheduleID"        SERIAL          PRIMARY KEY,
    "TenantID"          INTEGER         NOT NULL DEFAULT 1,

    "Title"             VARCHAR(200)    NOT NULL,
    "Body"              TEXT            NOT NULL,

    -- 'custom'            → free-form admin message
    -- 'campaign_open'     → auto-fired when a campaign opens
    -- 'campaign_reminder' → auto-fired N days before close
    -- 'campaign_final'    → auto-fired 24h before close
    "NotificationType"  VARCHAR(50)     NOT NULL DEFAULT 'custom',

    -- Audience spec — examples:
    --   {"user_type": "Researcher"}                    → all researchers
    --   {"department_id": [3, 5]}                      → two specific depts
    --   {"user_ids": [12, 34, 56]}                     → explicit list
    --   {"role": "HoD"}                                → all HoDs
    -- The worker translates this to a SELECT against Users.
    "TargetAudience"    JSONB           NOT NULL,

    "SendAt"            TIMESTAMPTZ     NOT NULL,

    -- pending    → waiting for SendAt
    -- processing → worker has claimed this row (FOR UPDATE SKIP LOCKED)
    -- sent       → fan-out completed
    -- failed     → terminal failure (ErrorMessage explains)
    -- cancelled  → admin cancelled before send
    "Status"            VARCHAR(20)     NOT NULL DEFAULT 'pending',

    "SentAt"            TIMESTAMPTZ,
    "RecipientCount"    INTEGER,
    "ErrorMessage"      TEXT,

    "RelatedCampaignID" INTEGER         REFERENCES "ReportCampaign"("CampaignID") ON DELETE CASCADE,
    "CreatedByUserID"   INTEGER         REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "CreatedAt"         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_notif_status
        CHECK ("Status" IN ('pending', 'processing', 'sent', 'failed', 'cancelled'))
);

-- Hot path for the worker: "which rows are due now?"
CREATE INDEX IF NOT EXISTS idx_notif_due
    ON "ScheduledNotification" ("SendAt")
    WHERE "Status" = 'pending';

CREATE INDEX IF NOT EXISTS idx_notif_tenant_status
    ON "ScheduledNotification" ("TenantID", "Status");


-- ---------------------------------------------------------------------------
-- 5. New permissions for campaigns + notifications
-- ---------------------------------------------------------------------------
-- These plug into the existing RBAC. Admin role gets them automatically.
-- HoD/Dean can be granted manage_campaigns + view_campaign_reports later
-- via the Role Management UI without touching this migration.
-- ---------------------------------------------------------------------------
INSERT INTO "Permission" ("Code", "Description", "Category")
VALUES
    ('manage_campaigns',
     'Create, edit, open, close, and archive report campaigns',
     'campaigns'),
    ('view_campaign_reports',
     'View campaign submissions and aggregated decisions',
     'campaigns'),
    ('compose_notifications',
     'Compose and schedule outbound notifications',
     'notifications')
ON CONFLICT ("Code") DO NOTHING;

-- Wire the new perms to the Admin role for the default tenant.
-- Idempotent — re-running is a no-op.
INSERT INTO "RolePermission" ("RoleID", "PermissionID")
SELECT r."RoleID", p."PermissionID"
  FROM "Role"       r
  CROSS JOIN "Permission" p
 WHERE r."Name"     = 'Admin'
   AND r."TenantID" = 1
   AND p."Code" IN (
       'manage_campaigns',
       'view_campaign_reports',
       'compose_notifications'
   )
ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------------
-- Sanity output — uncomment to see counts after the migration runs.
-- ---------------------------------------------------------------------------
-- SELECT 'ReportCampaign'        AS tbl, COUNT(*) FROM "ReportCampaign"
-- UNION ALL
-- SELECT 'ReportSubmission'      AS tbl, COUNT(*) FROM "ReportSubmission"
-- UNION ALL
-- SELECT 'ReportPaperDecision'   AS tbl, COUNT(*) FROM "ReportPaperDecision"
-- UNION ALL
-- SELECT 'ScheduledNotification' AS tbl, COUNT(*) FROM "ScheduledNotification";
