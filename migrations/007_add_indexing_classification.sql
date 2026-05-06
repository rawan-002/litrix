


BEGIN;

ALTER TABLE "ResearchPaper"
    ADD COLUMN IF NOT EXISTS "Indexing" VARCHAR(50);

CREATE INDEX IF NOT EXISTS "ix_research_paper_indexing"
    ON "ResearchPaper" ("Indexing")
    WHERE "Indexing" IS NOT NULL;


UPDATE "ResearchPaper"
SET "Indexing" = "RawData_Log"->>'indexing'
WHERE "Indexing" IS NULL
  AND "RawData_Log"->>'indexing' IS NOT NULL;

COMMIT;


