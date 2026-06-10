-- ============================================================================
-- 20260607_dept_stats_split.sql
-- Apply with: python tools/run_migration.py backend/migrations/20260607_dept_stats_split.sql
--
-- WHAT THIS DOES
--   Extends v_department_stats with:
--     • journal_papers / conference_papers split (VenueType ILIKE 'Conference%'
--       → Conference; everything else incl. Book/Book Series/NULL → Journal)
--     • total_q2_papers / total_q3_papers / total_q4_papers (Q1 already existed)
--
--   It ALSO fixes the paper counting model. The previous view summed
--   per-researcher counts from v_researcher_stats:
--       COALESCE(sum(rs.total_papers), 0)  -- a paper co-authored by TWO
--                                          -- researchers in the SAME dept
--                                          -- counted TWICE for that dept
--   The new definition counts papers with COUNT(DISTINCT "PaperID") per
--   department (FULL counting: a multi-department paper still counts once
--   for EACH department, matching /api/stats/overview/ and the Excel-era
--   recount_departments logic). total_citations keeps its previous
--   per-researcher-sum definition (author-level unification is a separate
--   decision — see OPERATIONS_LOG).
--
--   NOTE: the original view definition was never checked into the repo
--   ("migration 005" predates it). The previous body is preserved below for
--   reference; this file is now the canonical definition.
--
-- PREVIOUS DEFINITION (recovered from the live DB on 2026-06-07):
--   SELECT d."DepartmentID" AS department_id,
--       d."DepartmentName" AS department_name,
--       d."CollegeID" AS college_id,
--       count(DISTINCT rs.user_id) AS total_researchers,
--       count(DISTINCT rs.user_id) FILTER (WHERE rs.last_synced_at IS NOT NULL) AS active_researchers,
--       COALESCE(sum(rs.total_papers), 0::numeric) AS total_papers,
--       COALESCE(sum(rs.total_citations), 0::numeric) AS total_citations,
--       COALESCE(sum(rs.q1_papers), 0::numeric) AS total_q1_papers,
--       COALESCE(sum(rs.scopus_papers), 0::numeric) AS total_scopus_papers,
--       COALESCE(sum(rs.isi_papers), 0::numeric) AS total_isi_papers,
--       COALESCE(round(avg(rs.h_index), 1), 0::numeric) AS avg_h_index,
--       COALESCE(max(rs.h_index), 0) AS max_h_index
--   FROM "Department" d
--   LEFT JOIN v_researcher_stats rs ON rs.department_id = d."DepartmentID"
--   GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID";
-- ============================================================================

CREATE OR REPLACE VIEW v_department_stats AS
WITH dept_papers AS (
    -- One row per department with DISTINCT paper counting (FULL counting:
    -- a paper counts once per department it appears in, regardless of how
    -- many of that department's researchers co-authored it).
    SELECT
        w."DepartmentID" AS department_id,
        COUNT(DISTINCT rp."PaperID") AS total_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (
            WHERE j."VenueType" ILIKE 'Conference%') AS conference_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (
            WHERE j."VenueType" IS NULL
               OR j."VenueType" NOT ILIKE 'Conference%') AS journal_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile" = 'Q1') AS total_q1_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile" = 'Q2') AS total_q2_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile" = 'Q3') AS total_q3_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile" = 'Q4') AS total_q4_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (
            WHERE rp."Indexing" = 'Scopus' OR lr."Quartile" IS NOT NULL) AS total_scopus_papers,
        COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS total_isi_papers
    FROM "Works_In" w
    JOIN "Authors" a        ON a."UserID" = w."UserID"
    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
    LEFT JOIN "Journals" j  ON j."JournalID" = rp."JournalID"
    LEFT JOIN LATERAL (
        -- Latest ranking wins — same convention as analytics/views.py
        SELECT "Quartile"
        FROM "JournalRankings"
        WHERE "JournalID" = rp."JournalID"
        ORDER BY "RankingYear" DESC NULLS LAST, "Source"
        LIMIT 1
    ) lr ON TRUE
    WHERE w."IsCurrentPosition" = TRUE
    GROUP BY w."DepartmentID"
)
SELECT
    d."DepartmentID"   AS department_id,
    d."DepartmentName" AS department_name,
    d."CollegeID"      AS college_id,
    count(DISTINCT rs.user_id) AS total_researchers,
    count(DISTINCT rs.user_id) FILTER (WHERE rs.last_synced_at IS NOT NULL) AS active_researchers,
    COALESCE(dp.total_papers, 0)::numeric        AS total_papers,
    COALESCE(sum(rs.total_citations), 0::numeric) AS total_citations,
    COALESCE(dp.total_q1_papers, 0)::numeric     AS total_q1_papers,
    COALESCE(dp.total_scopus_papers, 0)::numeric AS total_scopus_papers,
    COALESCE(dp.total_isi_papers, 0)::numeric    AS total_isi_papers,
    COALESCE(round(avg(rs.h_index), 1), 0::numeric) AS avg_h_index,
    COALESCE(max(rs.h_index), 0) AS max_h_index,
    -- New columns (appended — CREATE OR REPLACE requires existing order kept)
    COALESCE(dp.journal_papers, 0)    AS journal_papers,
    COALESCE(dp.conference_papers, 0) AS conference_papers,
    COALESCE(dp.total_q2_papers, 0)   AS total_q2_papers,
    COALESCE(dp.total_q3_papers, 0)   AS total_q3_papers,
    COALESCE(dp.total_q4_papers, 0)   AS total_q4_papers
FROM "Department" d
LEFT JOIN v_researcher_stats rs ON rs.department_id = d."DepartmentID"
LEFT JOIN dept_papers dp        ON dp.department_id = d."DepartmentID"
GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID",
         dp.total_papers, dp.journal_papers, dp.conference_papers,
         dp.total_q1_papers, dp.total_q2_papers, dp.total_q3_papers,
         dp.total_q4_papers, dp.total_scopus_papers, dp.total_isi_papers;
