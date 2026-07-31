-- Exact name Google Scholar shows on a researcher's profile
-- (SerpAPI google_scholar_author -> author.name), captured verbatim at
-- scrape time. This is now the site's primary displayed name (see
-- citations/backfill_scholar_names.py); FullName_Ar stays in the table but
-- is never surfaced in the UI. Nullable: only researchers with a Scholar_ID
-- get one.
ALTER TABLE "Users" ADD COLUMN IF NOT EXISTS "ScholarDisplayName" text;
