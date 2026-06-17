-- Make the reporting views prefer the paper-level "ResearchPaper"."VenueType"
-- (set by classification/dblp_venue.py from Crossref + DBLP) over the
-- journal-level "Journals"."VenueType", which is unreliable because journals are
-- badly merged. Falls back to the journal value when the paper has none.
--
-- Recreates v_paper_details and v_department_stats. The JournalRankings LATERAL
-- de-duplication from 20260615 is preserved.

CREATE OR REPLACE VIEW v_paper_details AS
 WITH first_author AS (
         SELECT DISTINCT ON (a."PaperID") a."PaperID",
            a."UserID"
           FROM "Authors" a
          ORDER BY a."PaperID", a."AuthorOrder", a."UserID"
        ), albaha_authors AS (
         SELECT a."PaperID",
            string_agg(COALESCE(u."FullName_Ar", TRIM(BOTH FROM concat_ws(' '::text, u."FirstName", u."LastName"))::character varying)::text, '، '::text ORDER BY a."AuthorOrder") AS authors_ar,
            string_agg(COALESCE(u."FullName_Ar", TRIM(BOTH FROM concat_ws(' '::text, u."FirstName", u."LastName"))::character varying)::text || ' (جامعة الباحة)'::text, '، '::text ORDER BY a."AuthorOrder") AS authors_with_affil
           FROM "Authors" a
             JOIN "Users" u ON u."UserID" = a."UserID"
          GROUP BY a."PaperID"
        ), external_authors AS (
         SELECT ea."PaperID",
            string_agg(COALESCE(ea."FullName", ''::character varying)::text || COALESCE((' ('::text || ea."Affiliation"::text) || ')'::text, ''::text), '، '::text) AS authors_with_affil
           FROM "ExternalAuthors" ea
          GROUP BY ea."PaperID"
        ), raw_scraped_authors AS (
         SELECT rp_1."PaperID",
                CASE
                    WHEN jsonb_typeof(rp_1."RawData_Log" -> 'authors'::text) = 'string'::text THEN rp_1."RawData_Log" ->> 'authors'::text
                    WHEN jsonb_typeof(rp_1."RawData_Log" -> 'authors'::text) = 'array'::text THEN ( SELECT string_agg(COALESCE(jsonb_array_elements.value ->> 'name'::text, jsonb_array_elements.value ->> 'display_name'::text, jsonb_array_elements.value::text), ', '::text) AS string_agg
                       FROM jsonb_array_elements(rp_1."RawData_Log" -> 'authors'::text) jsonb_array_elements(value))
                    WHEN jsonb_typeof(rp_1."RawData_Log" -> 'authorships'::text) = 'array'::text THEN ( SELECT string_agg((jsonb_array_elements.value -> 'author'::text) ->> 'display_name'::text, ', '::text) AS string_agg
                       FROM jsonb_array_elements(rp_1."RawData_Log" -> 'authorships'::text) jsonb_array_elements(value))
                    ELSE NULL::text
                END AS authors_raw
           FROM "ResearchPaper" rp_1
        )
 SELECT rp."PaperID" AS paper_id,
    rp."Title" AS title,
    rp."PubYear" AS pub_year,
    rp."DOI" AS doi,
    rp."Source" AS source,
    rp."Indexing" AS indexing,
    pc.citations,
    j."JournalName" AS journal_name,
    COALESCE(rp."VenueType", j."VenueType") AS venue_type,
    jr."Quartile" AS quartile,
    jr."ImpactFactor" AS impact_factor,
    fa."UserID" AS first_author_user_id,
    d."DepartmentID" AS department_id,
    d."DepartmentName" AS department_name,
    aa.authors_ar,
    aa.authors_with_affil AS albaha_authors,
    ext.authors_with_affil AS external_authors,
    NULLIF(TRIM(BOTH FROM rsa.authors_raw), ''::text) AS all_authors_en,
    rsa.authors_raw AS all_authors_combined
   FROM "ResearchPaper" rp
     LEFT JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
     LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
     LEFT JOIN LATERAL (
         SELECT "Quartile", "ImpactFactor"
           FROM "JournalRankings"
          WHERE "JournalID" = rp."JournalID"
          ORDER BY "RankingYear" DESC NULLS LAST, "Source"
          LIMIT 1
     ) jr ON TRUE
     LEFT JOIN first_author fa ON fa."PaperID" = rp."PaperID"
     LEFT JOIN "Works_In" w ON w."UserID" = fa."UserID" AND w."IsCurrentPosition" = true
     LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
     LEFT JOIN albaha_authors aa ON aa."PaperID" = rp."PaperID"
     LEFT JOIN external_authors ext ON ext."PaperID" = rp."PaperID"
     LEFT JOIN raw_scraped_authors rsa ON rsa."PaperID" = rp."PaperID";

CREATE OR REPLACE VIEW v_department_stats AS
 WITH dept_papers AS (
         SELECT w."DepartmentID" AS department_id,
            count(DISTINCT rp."PaperID") AS total_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE COALESCE(rp."VenueType", j."VenueType")::text ~~* 'Conference%'::text) AS conference_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE COALESCE(rp."VenueType", j."VenueType") IS NULL OR COALESCE(rp."VenueType", j."VenueType")::text !~~* 'Conference%'::text) AS journal_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q1'::text) AS total_q1_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q2'::text) AS total_q2_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q3'::text) AS total_q3_papers,
            count(DISTINCT rp."PaperID") FILTER (WHERE lr."Quartile"::text = 'Q4'::text) AS total_q4_papers,
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
