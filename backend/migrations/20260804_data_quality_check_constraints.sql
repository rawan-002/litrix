-- Data-quality guardrails at the schema level, so invalid values are
-- rejected by Postgres itself rather than relying on Django/pipeline
-- code alone to keep them out. Companion to
-- 20260804_unique_normalized_issn.sql (same "application validates,
-- database guarantees" principle).
--
-- Every constraint here was checked against production data BEFORE
-- being written, specifically to avoid the trap of a CHECK that looks
-- reasonable in the abstract but rejects real, valid rows:
--   - JournalRankings.Quartile allows '-' alongside NULL/Q1-Q4 because
--     that's SCImago's own "no assigned quartile in this category" value
--     for ~2,300 catalog rows (mostly RankingID rows not yet linked to
--     any Journals row) -- NOT bad data. A naive `IN ('Q1'..'Q4')` check
--     would have broken every future SCImago import.
--   - The PubYear upper bound is a generous fixed literal (2035), not a
--     NOW()-derived expression -- CHECK constraints re-evaluate on every
--     UPDATE of the row (not just when the checked column changes), so a
--     year-relative bound would make old rows start failing their own
--     CHECK the moment the wall clock crosses whatever bound was live at
--     insert time.

ALTER TABLE "JournalRankings"
    ADD CONSTRAINT chk_journalrankings_quartile
    CHECK ("Quartile" IS NULL OR "Quartile" IN ('Q1', 'Q2', 'Q3', 'Q4', '-'));

ALTER TABLE "JournalRankings"
    ADD CONSTRAINT chk_journalrankings_impactfactor_nonneg
    CHECK ("ImpactFactor" IS NULL OR "ImpactFactor" >= 0);

ALTER TABLE "ResearchPaper"
    ADD CONSTRAINT chk_researchpaper_pubyear_range
    CHECK ("PubYear" IS NULL OR "PubYear" BETWEEN 1900 AND 2035);

ALTER TABLE "Citations"
    ADD CONSTRAINT chk_citations_count_nonneg
    CHECK ("CitationsCount" IS NULL OR "CitationsCount" >= 0);

ALTER TABLE "CitationsHistory"
    ADD CONSTRAINT chk_citationshistory_count_nonneg
    CHECK ("Count" IS NULL OR "Count" >= 0);

ALTER TABLE "Researcher"
    ADD CONSTRAINT chk_researcher_hindex_nonneg
    CHECK ("H_Index" IS NULL OR "H_Index" >= 0);

-- DOI uniqueness, normalized (lowercase + trim) -- mirrors the ISSN fix.
-- upsert_research_paper() in scopus_attribution_fix.py already matches
-- DOI case-insensitively (`LOWER("DOI") = LOWER(%s)`), but the DB-level
-- constraint was still the raw, case-sensitive "ResearchPaper_DOI_key" /
-- "uq_paper_doi" -- any write path that skipped that matching function
-- could still have slipped a case-duplicate past the database. Checked
-- production for existing case/whitespace-duplicate DOIs first: zero
-- found, so this is a pure backstop, not a fixup.
ALTER TABLE "ResearchPaper" DROP CONSTRAINT IF EXISTS "ResearchPaper_DOI_key";
DROP INDEX IF EXISTS uq_paper_doi;
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_doi_normalized
    ON "ResearchPaper" ((lower(trim("DOI"))))
    WHERE "DOI" IS NOT NULL AND trim("DOI") != '';
