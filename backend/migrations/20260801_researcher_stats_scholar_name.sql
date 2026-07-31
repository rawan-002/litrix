-- Fix v_researcher_stats.full_name_en so it prefers ScholarDisplayName (the
-- English name the UI shows everywhere) before the FirstName+MiddleName+LastName
-- fallback. Previously full_name_en was ONLY concat_ws(First,Middle,Last), which
-- for most researchers holds the ARABIC name (FirstName='محمد' ...). The frontend
-- name helper hides non-Latin names, so the Departments page (and anything reading
-- this view) showed BLANK researcher names. Preferring ScholarDisplayName makes
-- the English name appear; the 37 not-yet-scraped researchers (no ScholarDisplayName
-- and Arabic first/last) still fall through to NULL until they get an English name.
--
-- Only full_name_en (+ its GROUP BY) changes; everything else matches
-- 20260731_add_preprint_to_venue_gate.sql.

CREATE OR REPLACE VIEW v_researcher_stats AS
 SELECT u."UserID" AS user_id,
    u."FullName_Ar" AS full_name_ar,
    COALESCE(
        NULLIF(u."ScholarDisplayName", ''::text),
        NULLIF(TRIM(BOTH FROM concat_ws(' '::text, u."FirstName", u."MiddleName", u."LastName")), ''::text)
    ) AS full_name_en,
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
  GROUP BY u."UserID", u."FullName_Ar", u."ScholarDisplayName", u."FirstName", u."MiddleName", u."LastName", u."Scholar_ID", r."ORCID_ID", r."OpenAlex_AuthorID", r."AcademicRank", r."LastSyncedAt", d."DepartmentID", d."DepartmentName", hi.h_index;
