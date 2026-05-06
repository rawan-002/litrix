


BEGIN;

ALTER TABLE "ResearchPaper"
    ADD COLUMN IF NOT EXISTS "CitationsByYear" JSONB;

CREATE INDEX IF NOT EXISTS "ix_research_paper_citations_by_year"
    ON "ResearchPaper" USING GIN ("CitationsByYear");

COMMIT;


