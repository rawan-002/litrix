-- =============================================================================
-- Sprint 8 — Author Reconciliation Queue
-- =============================================================================
-- Adds the AuthorReviewQueue table that holds low-confidence co-author
-- matches surfaced by analytics.disambiguation.pipeline.
--
-- Why this exists
-- ---------------
-- The scraper writes high-confidence (≥0.70) co-author links straight into
-- "Authors". Anything below that threshold lands here so an admin can
-- confirm / reject without polluting the verified author graph.
--
-- Constraints
-- -----------
--   • One queue row per (PaperID, ScrapedName) — re-scraping the same paper
--     doesn't create duplicates.
--   • Status is a closed enum (CHECK constraint) — no free-text.
--   • Reviewer is recorded for audit (ReviewedByUserID + ReviewedAt).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS "AuthorReviewQueue" (
    "ReviewID"               SERIAL          PRIMARY KEY,
    "PaperID"                INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE CASCADE,
    "ScrapedName"            VARCHAR(255)    NOT NULL,
    "ScrapedAffiliation"     VARCHAR(255)    NULL,
    "SuggestedUserID"        INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "SuggestedConfidence"    NUMERIC(4,3)    NOT NULL DEFAULT 0,
    "SuggestedCriteria"      VARCHAR(50)     NOT NULL,
    "Status"                 VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
                             CHECK ("Status" IN ('PENDING','CONFIRMED','REJECTED','SKIPPED')),
    "ReviewedByUserID"       INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "ReviewedAt"             TIMESTAMPTZ     NULL,
    "ReviewerNotes"          TEXT            NULL,
    "CreatedAt"              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_queue_paper_scraped_name UNIQUE ("PaperID", "ScrapedName")
);

CREATE INDEX IF NOT EXISTS idx_review_status_created
    ON "AuthorReviewQueue" ("Status", "CreatedAt" DESC);

CREATE INDEX IF NOT EXISTS idx_review_suggested_user
    ON "AuthorReviewQueue" ("SuggestedUserID")
    WHERE "Status" = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_review_scraped_name_trgm
    ON "AuthorReviewQueue" USING GIN ("ScrapedName" gin_trgm_ops);
