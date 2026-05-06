


BEGIN;

CREATE OR REPLACE VIEW v_paper_details AS
WITH first_author AS (
    SELECT DISTINCT ON (a."PaperID")
        a."PaperID",
        a."UserID"
    FROM "Authors" a
    ORDER BY a."PaperID", a."AuthorOrder" NULLS LAST, a."UserID"
),
paper_authors AS (
    SELECT
        a."PaperID",
        STRING_AGG(
            COALESCE(u."FullName_Ar",
                     TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))),
            '، ' ORDER BY a."AuthorOrder" NULLS LAST
        ) AS authors_ar
    FROM "Authors" a
    JOIN "Users" u ON u."UserID" = a."UserID"
    GROUP BY a."PaperID"
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
    pa.authors_ar          AS authors_ar
FROM "ResearchPaper" rp
LEFT JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
LEFT JOIN first_author fa ON fa."PaperID" = rp."PaperID"
LEFT JOIN "Works_In" w
    ON w."UserID" = fa."UserID"
   AND w."IsCurrentPosition" = TRUE
LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
LEFT JOIN paper_authors pa ON pa."PaperID" = rp."PaperID";

COMMIT;
