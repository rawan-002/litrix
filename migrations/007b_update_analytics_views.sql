


BEGIN;


CREATE OR REPLACE VIEW v_researcher_stats AS
SELECT
    u."UserID"                                         AS user_id,
    u."FullName_Ar"                                    AS full_name_ar,
    NULLIF(TRIM(CONCAT_WS(' ',
        u."FirstName", u."MiddleName", u."LastName"
    )), '')                                            AS full_name_en,
    u."Scholar_ID"                                     AS scholar_id,
    r."ORCID_ID"                                       AS orcid_id,
    r."OpenAlex_AuthorID"                              AS openalex_author_id,
    r."AcademicRank"                                   AS academic_rank,
    r."LastSyncedAt"                                   AS last_synced_at,
    d."DepartmentID"                                   AS department_id,
    d."DepartmentName"                                 AS department_name,

    COUNT(DISTINCT a."PaperID")                        AS total_papers,
    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE pc."PubYear" >= EXTRACT(YEAR FROM CURRENT_DATE) - 4)
                                                       AS papers_last_5_years,

    COALESCE(SUM(pc.citations), 0)                     AS total_citations,
    CASE
        WHEN COUNT(DISTINCT a."PaperID") = 0 THEN 0
        ELSE ROUND(
            COALESCE(SUM(pc.citations), 0)::numeric
            / COUNT(DISTINCT a."PaperID"),
            2
        )
    END                                                AS avg_citations_per_paper,
    COALESCE(hi.h_index, 0)                            AS h_index,

    MIN(pc."PubYear")                                  AS first_pub_year,
    MAX(pc."PubYear")                                  AS last_pub_year,

    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE jr."Quartile" = 'Q1')            AS q1_papers,
    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE rp."Source" = 'Both')            AS cross_validated_papers,

    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE rp."Indexing" = 'Scopus')        AS scopus_papers,
    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE rp."Indexing" = 'ISI')           AS isi_papers,
    COUNT(DISTINCT a."PaperID")
        FILTER (WHERE rp."Source" = 'Manual')          AS manual_papers
FROM "Users" u
JOIN "Researcher" r ON r."UserID" = u."UserID"
LEFT JOIN "Works_In" w
    ON w."UserID" = u."UserID"
   AND w."IsCurrentPosition" = TRUE
LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
LEFT JOIN v_paper_citations pc ON pc."PaperID" = a."PaperID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
LEFT JOIN v_researcher_h_index hi ON hi."UserID" = u."UserID"
WHERE u."UserType" = 'Researcher'
GROUP BY
    u."UserID", u."FullName_Ar", u."FirstName", u."MiddleName", u."LastName",
    u."Scholar_ID", r."ORCID_ID", r."OpenAlex_AuthorID",
    r."AcademicRank", r."LastSyncedAt",
    d."DepartmentID", d."DepartmentName",
    hi.h_index;


CREATE OR REPLACE VIEW v_top_papers AS
SELECT
    rp."PaperID"                                       AS paper_id,
    rp."Title"                                         AS title,
    rp."PubYear"                                       AS pub_year,
    rp."DOI"                                           AS doi,
    rp."Source"                                        AS source,
    rp."Indexing"                                      AS indexing,
    pc.citations                                       AS citations,
    j."JournalName"                                    AS journal_name,
    jr."Quartile"                                      AS quartile,
    jr."ImpactFactor"                                  AS impact_factor,
    (
        SELECT u."FullName_Ar"
        FROM "Authors" a
        JOIN "Users" u ON u."UserID" = a."UserID"
        WHERE a."PaperID" = rp."PaperID"
        ORDER BY a."AuthorOrder" NULLS LAST, a."UserID"
        LIMIT 1
    )                                                  AS primary_author_ar,
    rp."ScrapedAt"                                     AS scraped_at
FROM "ResearchPaper" rp
JOIN v_paper_citations pc ON pc."PaperID" = rp."PaperID"
LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID";


CREATE OR REPLACE VIEW v_department_stats AS
SELECT
    d."DepartmentID"                                   AS department_id,
    d."DepartmentName"                                 AS department_name,
    d."CollegeID"                                      AS college_id,
    COUNT(DISTINCT rs.user_id)                         AS total_researchers,
    COUNT(DISTINCT rs.user_id)
        FILTER (WHERE rs.last_synced_at IS NOT NULL)   AS active_researchers,
    COALESCE(SUM(rs.total_papers), 0)                  AS total_papers,
    COALESCE(SUM(rs.total_citations), 0)              AS total_citations,
    COALESCE(SUM(rs.q1_papers), 0)                     AS total_q1_papers,
    COALESCE(SUM(rs.scopus_papers), 0)                 AS total_scopus_papers,
    COALESCE(SUM(rs.isi_papers), 0)                    AS total_isi_papers,
    COALESCE(ROUND(AVG(rs.h_index)::numeric, 1), 0)    AS avg_h_index,
    COALESCE(MAX(rs.h_index), 0)                       AS max_h_index
FROM "Department" d
LEFT JOIN v_researcher_stats rs ON rs.department_id = d."DepartmentID"
GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID";

COMMIT;
