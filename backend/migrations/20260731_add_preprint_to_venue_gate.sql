-- Extend the venue gate to also exclude 'Preprint' (in addition to 'Book').
-- A new 'Preprint' VenueType was introduced for non-peer-reviewed rows
-- (preprints.org, arXiv). Like Book chapters, preprints must NOT count as
-- journal publications or toward Q1-Q4 KPIs. This recreates the same three
-- views as 20260702_venue_gate_quartile_kpis.sql, changing every
--   <> 'Book'   ->   NOT IN ('Book', 'Preprint')
-- Journal-eligible  <=>  venue IS NULL OR (venue NOT ILIKE 'Conference%' AND
--                                          venue NOT IN ('Book','Preprint')).
-- scopus_papers is still intentionally left ungated (Scopus indexes conferences).

-- ---------------------------------------------------------------------------
-- v_department_stats
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_department_stats AS
 WITH dept_papers AS (
         SELECT w."DepartmentID" AS department_id,
            count(DISTINCT rp."PaperID") AS total_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE COALESCE(rp."VenueType", j."VenueType")::text ~~* 'Conference%'::text) AS conference_papers,
            count(DISTINCT rp."PaperID") FILTER (
                WHERE COALESCE(rp."VenueType", j."VenueType") IS NULL
                   OR (COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text
                       AND COALESCE(rp."VenueType", j."VenueType")::text NOT IN ('Book'::text, 'Preprint'::text))) AS journal_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q1'::text
                AND (COALESCE(rp."VenueType", j."VenueType") IS NULL
                     OR (COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text
                         AND COALESCE(rp."VenueType", j."VenueType")::text NOT IN ('Book'::text, 'Preprint'::text)))) AS total_q1_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q2'::text
                AND (COALESCE(rp."VenueType", j."VenueType") IS NULL
                     OR (COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text
                         AND COALESCE(rp."VenueType", j."VenueType")::text NOT IN ('Book'::text, 'Preprint'::text)))) AS total_q2_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q3'::text
                AND (COALESCE(rp."VenueType", j."VenueType") IS NULL
                     OR (COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text
                         AND COALESCE(rp."VenueType", j."VenueType")::text NOT IN ('Book'::text, 'Preprint'::text)))) AS total_q3_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q4'::text
                AND (COALESCE(rp."VenueType", j."VenueType") IS NULL
                     OR (COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text
                         AND COALESCE(rp."VenueType", j."VenueType")::text NOT IN ('Book'::text, 'Preprint'::text)))) AS total_q4_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing"::text = 'Scopus'::text OR lr."Quartile" IS NOT NULL) AS total_scopus_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing"::text = 'ISI'::text) AS total_isi_papers
           FROM "Works_In" w
             JOIN "Authors" a ON a."UserID" = w."UserID"
             JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
             LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
             LEFT JOIN LATERAL ( SELECT "JournalRankings"."Quartile"
                   FROM "JournalRankings"
                  WHERE "JournalRankings"."JournalID" = rp."JournalID"
                  ORDER BY "JournalRankings"."RankingYear" DESC NULLS LAST, "JournalRankings"."Source"
                 LIMIT 1) lr ON true
          WHERE w."IsCurrentPosition" = true
          GROUP BY w."DepartmentID"
        )
 SELECT d."DepartmentID" AS department_id,
    d."DepartmentName" AS department_name,
    d."CollegeID" AS college_id,
    count(DISTINCT rs.user_id) AS total_researchers,
    count(DISTINCT rs.user_id) FILTER (WHERE rs.last_synced_at IS NOT NULL) AS active_researchers,
    COALESCE(dp.total_papers, 0::bigint)::numeric AS total_papers,
    COALESCE(sum(rs.total_citations), 0::numeric) AS total_citations,
    COALESCE(dp.total_q1_papers, 0::bigint)::numeric AS total_q1_papers,
    COALESCE(dp.total_scopus_papers, 0::bigint)::numeric AS total_scopus_papers,
    COALESCE(dp.total_isi_papers, 0::bigint)::numeric AS total_isi_papers,
    COALESCE(round(avg(rs.h_index), 1), 0::numeric) AS avg_h_index,
    COALESCE(max(rs.h_index), 0) AS max_h_index,
    COALESCE(dp.journal_papers, 0::bigint) AS journal_papers,
    COALESCE(dp.conference_papers, 0::bigint) AS conference_papers,
    COALESCE(dp.total_q2_papers, 0::bigint) AS total_q2_papers,
    COALESCE(dp.total_q3_papers, 0::bigint) AS total_q3_papers,
    COALESCE(dp.total_q4_papers, 0::bigint) AS total_q4_papers
   FROM "Department" d
     LEFT JOIN v_researcher_stats rs ON rs.department_id = d."DepartmentID"
     LEFT JOIN dept_papers dp ON dp.department_id = d."DepartmentID"
  GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID", dp.total_papers, dp.journal_papers, dp.conference_papers, dp.total_q1_papers, dp.total_q2_papers, dp.total_q3_papers, dp.total_q4_papers, dp.total_scopus_papers, dp.total_isi_papers;

-- ---------------------------------------------------------------------------
-- v_researcher_stats  (paper-level VenueType only)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_researcher_stats AS
 SELECT u."UserID" AS user_id,
    u."FullName_Ar" AS full_name_ar,
    NULLIF(TRIM(BOTH FROM concat_ws(' '::text, u."FirstName", u."MiddleName", u."LastName")), ''::text) AS full_name_en,
    u."Scholar_ID" AS scholar_id,
    r."ORCID_ID" AS orcid_id,
    r."OpenAlex_AuthorID" AS openalex_author_id,
    r."AcademicRank" AS academic_rank,
    r."LastSyncedAt" AS last_synced_at,
    d."DepartmentID" AS department_id,
    d."DepartmentName" AS department_name,
    count(DISTINCT a."PaperID") AS total_papers,
    count(DISTINCT a."PaperID") FILTER (WHERE pc."PubYear"::numeric >= (EXTRACT(year FROM CURRENT_DATE) - 4::numeric)) AS papers_last_5_years,
    COALESCE(sum(pc.citations), 0::bigint) AS total_citations,
        CASE
            WHEN count(DISTINCT a."PaperID") = 0 THEN 0::numeric
            ELSE round(COALESCE(sum(pc.citations), 0::bigint)::numeric / count(DISTINCT a."PaperID")::numeric, 2)
        END AS avg_citations_per_paper,
    COALESCE(hi.h_index, 0) AS h_index,
    min(pc."PubYear") AS first_pub_year,
    max(pc."PubYear") AS last_pub_year,
    count(DISTINCT a."PaperID") FILTER (WHERE jr."Quartile"::text = 'Q1'::text
        AND (rp."VenueType" IS NULL
             OR (rp."VenueType"::text !~~* 'Conference%'::text AND rp."VenueType"::text NOT IN ('Book'::text, 'Preprint'::text)))) AS q1_papers,
    count(DISTINCT a."PaperID") FILTER (WHERE rp."Source"::text = 'Both'::text) AS cross_validated_papers,
    count(DISTINCT a."PaperID") FILTER (WHERE rp."Indexing"::text = 'Scopus'::text OR jr."Quartile" IS NOT NULL) AS scopus_papers,
    count(DISTINCT a."PaperID") FILTER (WHERE rp."Indexing"::text = 'ISI'::text) AS isi_papers,
    count(DISTINCT a."PaperID") FILTER (WHERE rp."Source"::text = 'Manual'::text) AS manual_papers
   FROM "Users" u
     JOIN "Researcher" r ON r."UserID" = u."UserID"
     LEFT JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = true
     LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
     LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
     LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
     LEFT JOIN v_paper_citations pc ON pc."PaperID" = a."PaperID"
     LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
     LEFT JOIN v_researcher_h_index hi ON hi."UserID" = u."UserID"
  WHERE u."UserType"::text = 'Researcher'::text
  GROUP BY u."UserID", u."FullName_Ar", u."FirstName", u."MiddleName", u."LastName", u."Scholar_ID", r."ORCID_ID", r."OpenAlex_AuthorID", r."AcademicRank", r."LastSyncedAt", d."DepartmentID", d."DepartmentName", hi.h_index;

-- ---------------------------------------------------------------------------
-- v_publication_trends
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_publication_trends AS
 SELECT d."DepartmentID" AS department_id,
    d."DepartmentName" AS department_name,
    pc."PubYear" AS year,
    count(DISTINCT pc."PaperID") AS papers,
    COALESCE(sum(pc.citations), 0::bigint) AS citations,
    count(DISTINCT pc."PaperID") FILTER (WHERE jr."Quartile"::text = 'Q1'::text
        AND (rpv."VenueType" IS NULL
             OR (rpv."VenueType"::text !~~* 'Conference%'::text AND rpv."VenueType"::text NOT IN ('Book'::text, 'Preprint'::text)))) AS q1_papers
   FROM v_paper_citations pc
     JOIN "Authors" a ON a."PaperID" = pc."PaperID"
     JOIN "Works_In" w ON w."UserID" = a."UserID" AND w."IsCurrentPosition" = true
     JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
     LEFT JOIN "ResearchPaper" rpv ON rpv."PaperID" = pc."PaperID"
     LEFT JOIN "JournalRankings" jr ON jr."JournalID" = pc."JournalID"
  WHERE pc."PubYear" IS NOT NULL
  GROUP BY d."DepartmentID", d."DepartmentName", pc."PubYear";
