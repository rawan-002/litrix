-- Per-researcher citations breakdown straight from Scholar's cited_by.graph.
-- Profile page reads this for the "Citations per Year" chart, avoiding
-- the inaccuracy of summing per-paper CitationsByYear (which depends on
-- OpenAlex matches that may be wrong for common author names).

BEGIN;

ALTER TABLE "Researcher"
    ADD COLUMN IF NOT EXISTS "CitationsByYear" JSONB;

COMMIT;
