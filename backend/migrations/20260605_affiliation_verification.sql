-- =============================================================================
-- AFFILIATION VERIFICATION SCHEMA
-- =============================================================================
-- Adds 4 columns to ResearchPaper to track which papers have been verified
-- as actually authored under Al-Baha University affiliation (vs. attributed
-- to an Al-Baha researcher who at the time was at a different institution).
--
-- WHY THIS MATTERS (Architectural Context):
--   The Scholar scraper imports all papers from a researcher's profile,
--   including papers they wrote BEFORE joining Al-Baha (PhD work elsewhere,
--   postdoc at other institutions, sabbatical co-authorships). For NCAAA
--   reporting & accurate research-output metrics, we must distinguish:
--     • Papers WHERE the author was at Al-Baha at publication time → COUNT
--     • Papers where the author had other affiliation              → EXCLUDE
--
-- USAGE:
--   1. AffiliationVerified IS NULL → never checked
--   2. AffiliationVerified = TRUE  → confirmed Al-Baha (use in stats)
--   3. AffiliationVerified = FALSE → confirmed NOT Al-Baha (exclude)
--
-- ROLLBACK:
--   The ALTER TABLE ... DROP COLUMN statements at the bottom are commented
--   out — uncomment + run if you ever need to undo this migration.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Add the 4 verification columns
-- ---------------------------------------------------------------------------
ALTER TABLE "ResearchPaper"
    ADD COLUMN IF NOT EXISTS "AffiliationVerified"  BOOLEAN     DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS "VerificationSource"   VARCHAR(20) DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS "VerifiedAt"           TIMESTAMP   DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS "VerificationDetails"  JSONB       DEFAULT NULL;

-- Document expected VerificationSource values (informational comment)
COMMENT ON COLUMN "ResearchPaper"."VerificationSource" IS
    'Source that confirmed/denied affiliation. Allowed values:
       import-scopus     -- trusted at import time (Scopus AF-ID filter)
       import-openalex   -- trusted at import time (OpenAlex ROR)
       openalex          -- verified via OpenAlex API by DOI lookup
       crossref          -- verified via Crossref API by DOI lookup
       pdf               -- verified by downloading PDF + text extraction
       manual            -- human reviewer marked it
       pending-review    -- all automated tiers failed; needs human eyes';

COMMENT ON COLUMN "ResearchPaper"."VerificationDetails" IS
    'JSON evidence: matched_author, matched_institution, ror, pdf_url, etc.
     Used for audit trail — never overwrite a verified row without preserving
     the previous decision in a separate audit log.';


-- ---------------------------------------------------------------------------
-- 2. Indexes for fast dashboard filtering
-- ---------------------------------------------------------------------------
-- Partial index: only papers WITH a verification decision matter for stats.
-- Saves space + query time vs. indexing NULL rows we don't read.
CREATE INDEX IF NOT EXISTS idx_paper_affiliation_verified
    ON "ResearchPaper" ("AffiliationVerified")
    WHERE "AffiliationVerified" IS NOT NULL;

-- For audit dashboards: "show me all papers Tier-X verified in last month"
CREATE INDEX IF NOT EXISTS idx_paper_verification_source
    ON "ResearchPaper" ("VerificationSource", "VerifiedAt" DESC)
    WHERE "VerificationSource" IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3. Pre-seed trusted imports (Tier 0 — no API calls needed)
-- ---------------------------------------------------------------------------
-- The diagnostic showed Scopus = 100% Al-Baha (filter ran at import time).
-- The diagnostic showed OpenAlex = 100% Al-Baha (ROR matches in raw_data).
-- We mark these immediately so the verifier script doesn't waste API quota.
UPDATE "ResearchPaper" rp
SET "AffiliationVerified"  = TRUE,
    "VerificationSource"   = 'import-scopus',
    "VerifiedAt"           = NOW(),
    "VerificationDetails"  = jsonb_build_object(
        'reason', 'Source=Scopus, imported via scopus_attribution_fix with AF-ID(60104698) filter'
    )
WHERE rp."Source" = 'Scopus'
  AND rp."AffiliationVerified" IS NULL;

UPDATE "ResearchPaper" rp
SET "AffiliationVerified"  = TRUE,
    "VerificationSource"   = 'import-openalex',
    "VerifiedAt"           = NOW(),
    "VerificationDetails"  = jsonb_build_object(
        'reason', 'Source=OpenAlex, raw_data confirmed AlBaha ROR/affiliation'
    )
WHERE rp."Source" = 'OpenAlex'
  AND rp."AffiliationVerified" IS NULL;


-- ---------------------------------------------------------------------------
-- 4. Summary diagnostic (returns counts so you can verify the migration)
-- ---------------------------------------------------------------------------
SELECT
    rp."Source"                                              AS source,
    COUNT(*)                                                 AS total_papers,
    COUNT(*) FILTER (WHERE rp."AffiliationVerified" = TRUE)  AS verified_albaha,
    COUNT(*) FILTER (WHERE rp."AffiliationVerified" = FALSE) AS verified_not_albaha,
    COUNT(*) FILTER (WHERE rp."AffiliationVerified" IS NULL) AS pending
FROM "ResearchPaper" rp
JOIN "Authors" a ON a."PaperID" = rp."PaperID"
WHERE rp."PubYear" = ANY(ARRAY[2025, 2026])
GROUP BY rp."Source"
ORDER BY total_papers DESC;


COMMIT;


-- =============================================================================
-- ROLLBACK (uncomment + run if needed):
-- =============================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS idx_paper_verification_source;
-- DROP INDEX IF EXISTS idx_paper_affiliation_verified;
-- ALTER TABLE "ResearchPaper"
--     DROP COLUMN IF EXISTS "VerificationDetails",
--     DROP COLUMN IF EXISTS "VerifiedAt",
--     DROP COLUMN IF EXISTS "VerificationSource",
--     DROP COLUMN IF EXISTS "AffiliationVerified";
-- COMMIT;
