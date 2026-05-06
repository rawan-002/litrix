-- v2: prefer publication_info.authors[].name (full names) over summary
-- (which has initials like "MMM Hassan").

BEGIN;

DROP VIEW IF EXISTS v_paper_details CASCADE;

CREATE VIEW v_paper_details AS
WITH first_author AS (
    SELECT DISTINCT ON (a."PaperID") a."PaperID", a."UserID"
    FROM "Authors" a
    ORDER BY a."PaperID", a."AuthorOrder" NULLS LAST, a."UserID"
),
albaha_authors AS (
    SELECT
        a."PaperID",
        STRING_AGG(
            COALESCE(u."FullName_Ar",
                     TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))),
            '، ' ORDER BY a."AuthorOrder" NULLS LAST
        ) AS authors_ar,
        STRING_AGG(
            COALESCE(u."FullName_Ar",
                     TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) ||
            ' (جامعة الباحة)',
            '، ' ORDER BY a."AuthorOrder" NULLS LAST
        ) AS authors_with_affil
    FROM "Authors" a
    JOIN "Users" u ON u."UserID" = a."UserID"
    GROUP BY a."PaperID"
),
external_authors AS (
    SELECT
        ea."PaperID",
        STRING_AGG(
            COALESCE(ea."FullName", '') ||
            COALESCE(' (' || ea."Affiliation" || ')', ''),
            '، '
        ) AS authors_with_affil
    FROM "ExternalAuthors" ea
    GROUP BY ea."PaperID"
),
raw_scraped_authors AS (
    SELECT
        rp."PaperID",
        COALESCE(
            -- 1. SerpAPI Scholar: publication_info.authors[].name (full names)
            CASE
                WHEN jsonb_typeof(rp."RawData_Log"->'publication_info'->'authors') = 'array'
                 AND jsonb_array_length(rp."RawData_Log"->'publication_info'->'authors') > 0
                THEN (
                    SELECT STRING_AGG(value->>'name', ', ')
                    FROM jsonb_array_elements(
                        rp."RawData_Log"->'publication_info'->'authors'
                    )
                    WHERE value->>'name' IS NOT NULL
                )
                ELSE NULL
            END,
            -- 2. SerpAPI Scholar: publication_info.summary (initials, but raw)
            CASE
                WHEN rp."RawData_Log"->'publication_info'->>'summary' IS NOT NULL
                THEN TRIM(SPLIT_PART(
                    rp."RawData_Log"->'publication_info'->>'summary',
                    ' - ', 1
                ))
                ELSE NULL
            END,
            -- 3. OpenAlex: authorships[].author.display_name
            CASE
                WHEN jsonb_typeof(rp."RawData_Log"->'authorships') = 'array'
                THEN (
                    SELECT STRING_AGG(
                        value->'author'->>'display_name',
                        ', '
                    )
                    FROM jsonb_array_elements(rp."RawData_Log"->'authorships')
                )
                ELSE NULL
            END
        ) AS authors_raw
    FROM "ResearchPaper" rp
)
SELECT
    rp."PaperID"           AS paper_id,
    rp."Title"             AS title,
    rp."PubYear"           AS pub_year,
    rp."DOI"               AS doi,
    rp."Source"            AS source,
    rp."Indexing"          AS indexing,
    pc.citations           AS citations,
    j."JournalName"        AS journal_name,
    j."VenueType"          AS venue_type,
    jr."Quartile"          AS quartile,
    jr."ImpactFactor"      AS impact_factor,
    fa."UserID"            AS first_author_user_id,
    d."DepartmentID"       AS department_id,
    d."DepartmentName"     AS department_name,
    aa.authors_ar          AS authors_ar,
    aa.authors_with_affil  AS albaha_authors,
    ext.authors_with_affil AS external_authors,
    -- Raw scraped author list. NULL means RawData_Log doesn't have it.
    NULLIF(TRIM(rsa.authors_raw), '') AS all_authors_en,
    rsa.authors_raw                    AS all_authors_combined
FROM "ResearchPaper" rp
LEFT JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
LEFT JOIN first_author fa ON fa."PaperID" = rp."PaperID"
LEFT JOIN "Works_In" w ON w."UserID" = fa."UserID" AND w."IsCurrentPosition" = TRUE
LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
LEFT JOIN albaha_authors aa ON aa."PaperID" = rp."PaperID"
LEFT JOIN external_authors ext ON ext."PaperID" = rp."PaperID"
LEFT JOIN raw_scraped_authors rsa ON rsa."PaperID" = rp."PaperID";

COMMIT;
