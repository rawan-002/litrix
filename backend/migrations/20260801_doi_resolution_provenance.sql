-- Provenance for DOIs recovered by title-search (affiliation_verifier.py P1):
-- when a paper had no DOI and one was inferred via Crossref/OpenAlex title
-- lookup, we record HOW ('crossref-title' / 'openalex-title') and WHEN, so an
-- inferred DOI is never mistaken for an original one during audit.
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "DoiResolvedBy"  VARCHAR(40);
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "DoiResolvedAt"  TIMESTAMPTZ;
