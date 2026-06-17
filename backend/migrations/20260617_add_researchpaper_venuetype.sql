-- Paper-level venue type (Journal / Conference), populated by
-- classification/dblp_venue.py from Crossref (by DOI) + DBLP (authoritative for
-- CS conferences published inside serials like Procedia / LNCS, and for papers
-- without a DOI).
--
-- This lives on the PAPER, not the Journal, because the Journals table has
-- badly-merged rows (one record holding papers from several real venues), so a
-- journal-level VenueType cannot be trusted for these. The reporting views
-- COALESCE(rp."VenueType", j."VenueType") so a paper-level verdict wins.

ALTER TABLE "ResearchPaper" ADD COLUMN IF NOT EXISTS "VenueType" VARCHAR(20);
