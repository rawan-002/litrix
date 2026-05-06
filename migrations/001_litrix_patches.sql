


BEGIN;


ALTER TABLE "ResearchPaper"
    ADD COLUMN IF NOT EXISTS "NormalizedTitle" TEXT;


UPDATE "ResearchPaper"
SET "NormalizedTitle" = LOWER(TRIM(REGEXP_REPLACE(
    REGEXP_REPLACE("Title", '[^[:alnum:][:space:]]', ' ', 'g'),
    '\s+', ' ', 'g'
)))
WHERE "NormalizedTitle" IS NULL AND "Title" IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS "uq_paper_normalized_title"
    ON "ResearchPaper" ("NormalizedTitle")
    WHERE "NormalizedTitle" IS NOT NULL;


CREATE UNIQUE INDEX IF NOT EXISTS "uq_paper_doi"
    ON "ResearchPaper" ("DOI")
    WHERE "DOI" IS NOT NULL;


CREATE UNIQUE INDEX IF NOT EXISTS "uq_authors_user_paper"
    ON "Authors" ("UserID", "PaperID");


CREATE UNIQUE INDEX IF NOT EXISTS "uq_external_author_paper"
    ON "ExternalAuthors" ("FullName", "PaperID");


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_researcher_userid'
    ) THEN
        ALTER TABLE "Researcher"
        ADD CONSTRAINT "uq_researcher_userid" UNIQUE ("UserID");
    END IF;
END $$;


CREATE INDEX IF NOT EXISTS "ix_users_scholar_id"
    ON "Users" ("Scholar_ID");

CREATE INDEX IF NOT EXISTS "ix_journals_issn_print"
    ON "Journals" ("ISSN_Print");

CREATE INDEX IF NOT EXISTS "ix_journals_issn_online"
    ON "Journals" ("ISSN_Online");

CREATE INDEX IF NOT EXISTS "ix_paper_journal_year"
    ON "ResearchPaper" ("JournalID", "PubYear");

CREATE INDEX IF NOT EXISTS "ix_issn_mapping_issn"
    ON "ISSN_Mapping" ("ISSN");

COMMIT;


