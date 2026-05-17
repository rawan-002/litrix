-- ============================================================
-- Sprint 9 — Researcher.ResearchInterests
-- ============================================================
-- Adds a JSONB column to store the Google-Scholar-style profile
-- labels each researcher chooses to represent their work, e.g.:
--   ["Artificial Intelligence",
--    "Computational Intelligence",
--    "Ambient Intelligence",
--    "Agent and Multi-agent"]
--
-- WHY JSONB (not a separate table):
--   • The list is short (<20 items per researcher), read-mostly.
--   • A GIN index gives us O(log N) set-overlap lookups for the
--     "shared interests" matching in network_views.py.
--   • Avoids a 3rd join (Researcher × ResearchInterest × ...)
--     when we just need set arithmetic per author.
--
-- WHY NOT NORMALISE AS Keywords + ResearcherKeyword:
--   • Each researcher's labels are personal — no global vocabulary
--     to enforce. Two researchers writing "AI" and "Artificial
--     Intelligence" are still distinct expressions.
--   • Author Name Disambiguation already handles canonicalisation
--     of *researchers*; we don't need to also canonicalise *labels*.
--   • Search/match still works via lower(value) comparison on the
--     unnested array — see network_views.INTEREST_NEIGHBOURS_SQL.
-- ============================================================

ALTER TABLE "Researcher"
ADD COLUMN IF NOT EXISTS "ResearchInterests" jsonb DEFAULT NULL;

COMMENT ON COLUMN "Researcher"."ResearchInterests" IS
'JSONB array of Scholar-style interest labels. e.g. ["Artificial Intelligence", "Computational Intelligence"]. Source: Google Scholar profile labels OR researcher-edited from profile UI.';

-- Track when interests were last refreshed so the management command
-- knows whether to re-fetch from Scholar.
ALTER TABLE "Researcher"
ADD COLUMN IF NOT EXISTS "ResearchInterestsUpdatedAt" timestamptz DEFAULT NULL;

-- GIN index → fast set containment + overlap lookups.
-- Powers the "researchers who share at least one label with X" query.
CREATE INDEX IF NOT EXISTS "Researcher_ResearchInterests_gin_idx"
ON "Researcher" USING gin ("ResearchInterests");
