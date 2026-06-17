-- Fix duplicate paper rows in v_top_papers and v_paper_details.
--
-- "JournalRankings" holds multiple rows per "JournalID" (one per RankingYear /
-- Source), so the plain `LEFT JOIN "JournalRankings"` in these two views
-- multiplied every paper by its number of ranking rows (commonly x3). This
-- surfaced as duplicated entries in the "Most-cited Papers" list and inflated
-- the yearly-breakdown citation sums.
--
-- Fix: pick a single ranking per journal via LEFT JOIN LATERAL ... LIMIT 1,
-- preferring the most recent RankingYear (matches the pattern already used in
-- analytics/exports.py). Column lists are unchanged, so CREATE OR REPLACE is safe.

CREATE OR REPLACE VIEW v_top_papers AS
 SELECT rp."PaperID" AS paper_id,
    rp."Title" AS title,
    rp."PubYear" AS pub_year,
    rp."DOI" AS doi,
    rp."Source" AS source,
    rp."Indexing" AS indexing,
    pc.citations,
    pc.citations_by_year,
    j."JournalName" AS journal_name,
    jr."Quartile" AS quartile,
    jr."ImpactFactor" AS impact_factor,
    ( SELECT u."FullName_Ar"
           FROM "Authors" a
             JOIN "Users" u ON u."UserID" = a."UserID"
          WHERE a."PaperID" = rp."PaperID"
          ORDER BY a."AuthorOrder", a."UserID"
         LIMIT 1) AS primary_author_ar,
    rp."ScrapedAt" AS scraped_at
   FROM "ResearchPaper" rp
     JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
     LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
     LEFT JOIN LATERAL (
         SELECT "Quartile", "ImpactFactor"
           FROM "JournalRankings"
          WHERE "JournalID" = rp."JournalID"
          ORDER BY "RankingYear" DESC NULLS LAST, "Source"
          LIMIT 1
     ) jr ON TRUE;

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
    j."VenueType" AS venue_type,
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
