-- Phase 6.5: separate "is this paper linked to an Al-Baha author" (AffiliationVerified)
-- from "is this record actually a research paper" (PublicationType). Discovered via
-- UID 69: 6 "Acknowledgment to Reviewers" journal notices were AffiliationVerified=True
-- (correctly - the researcher's own Scholar page lists them) but are not research output.
-- Nullable, additive, zero impact on existing rows/queries.
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "PublicationType" VARCHAR(30);

COMMENT ON COLUMN "ResearchPaper"."PublicationType" IS
  'One of: Research Article, Review, Conference Paper, Book Chapter, Editorial, Acknowledgment, Correction, Other. NULL = not yet classified.';
