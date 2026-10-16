-- Evidence/confidence for researcher identifiers and paper provenance
-- (scraping-pipeline redesign, see LITRIX_AI_CHATBOT.md-adjacent planning
-- notes). Nullable-only additions - zero effect on existing rows.
--
-- Researcher.IdentifierEvidence records, per identifier type (openalex_author_id,
-- orcid, scholar_id, scopus_id), WHERE it came from and how sure we are:
--   {"openalex_author_id": {"source": "csv_upload_2026-08-09",
--                            "confidence": "confirmed",
--                            "verified_at": "2026-08-09T...", "verified_by": "..."}}
-- One JSONB column (matching the existing style of ResearchInterests /
-- CitationsByYear / RawData_Log) rather than a source+confidence column pair
-- per identifier - four identifier types would mean eight near-empty columns.
ALTER TABLE "Researcher" ADD COLUMN IF NOT EXISTS "IdentifierEvidence" JSONB;

-- OpenAlexWorkID promoted to a first-class, queryable/joinable column -
-- previously only reachable via RawData_Log->>'openalex_id'.
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "OpenAlexWorkID" VARCHAR(60);

-- Which pipeline actually produced the stored Abstract text, so a copied
-- abstract is never silently treated as ground truth with no traceable origin.
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "AbstractSource" VARCHAR(20);

-- PdfUrl + PdfAccessType are deliberately separate from "do we have an
-- abstract" - a URL existing is NOT the same claim as "full text is
-- downloadable here". PdfAccessType is one of:
--   'downloaded_fulltext'      - we actually fetched and stored the file (not yet done by any script)
--   'open_access_pdf_link'     - OpenAlex marks is_oa=true AND gives a direct pdf_url
--   'open_access_landing_page' - is_oa=true but no direct pdf_url (a landing page only)
--   'publisher_page_only'      - not open access; whatever link exists is a paywalled page
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "PdfUrl" TEXT;
ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "PdfAccessType" VARCHAR(30);
