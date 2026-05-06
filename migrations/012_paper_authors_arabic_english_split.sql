-- Two-column author display for the Excel export:
--   1. authors_ar   — Al-Baha researchers in ARABIC (FullName_Ar)
--   2. all_authors_en — ALL authors (Al-Baha + external) in ENGLISH
--
-- For Al-Baha authors we build the English name from FirstName + LastName.
-- External authors are stored in English already (FullName).

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
        -- Arabic names (our system's primary representation)
        STRING_AGG(
            COALESCE(u."FullName_Ar",
                     TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))),
            '، ' ORDER BY a."AuthorOrder" NULLS LAST
        ) AS authors_ar,
        -- English names (FirstName + LastName)
        STRING_AGG(
            NULLIF(TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")), ''),
            ', ' ORDER BY a."AuthorOrder" NULLS LAST
        ) AS authors_en,
        -- Legacy: with affiliation suffix (kept for backward compatibility)
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
        ) AS authors_with_affil,
        STRING_AGG(
            COALESCE(ea."FullName", ''),
            ', '
        ) AS authors_clean
    FROM "ExternalAuthors" ea
    GROUP BY ea."PaperID"
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
    -- Column 1: Al-Baha researchers in Arabic (clean, no affiliation)
    aa.authors_ar          AS authors_ar,
    -- Legacy
    aa.authors_with_affil  AS albaha_authors,
    ext.authors_with_affil AS external_authors,
    -- Column 2: ALL authors (Al-Baha + external) in ENGLISH
    -- Order: Al-Baha first, then external. Comma-separated.
    TRIM(BOTH ', ' FROM
        COALESCE(aa.authors_en, '') ||
        CASE
            WHEN aa.authors_en IS NOT NULL AND ext.authors_clean IS NOT NULL
            THEN ', '
            ELSE ''
        END ||
        COALESCE(ext.authors_clean, '')
    ) AS all_authors_en,
    -- Backward-compat alias (some old code may still call this)
    TRIM(BOTH '، ' FROM
        COALESCE(aa.authors_ar, '') ||
        CASE
            WHEN aa.authors_ar IS NOT NULL AND ext.authors_clean IS NOT NULL
            THEN '، '
            ELSE ''
        END ||
        COALESCE(ext.authors_clean, '')
    ) AS all_authors_combined
FROM "ResearchPaper" rp
LEFT JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
LEFT JOIN first_author fa ON fa."PaperID" = rp."PaperID"
LEFT JOIN "Works_In" w ON w."UserID" = fa."UserID" AND w."IsCurrentPosition" = TRUE
LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
LEFT JOIN albaha_authors aa ON aa."PaperID" = rp."PaperID"
LEFT JOIN external_authors ext ON ext."PaperID" = rp."PaperID";

COMMIT;
