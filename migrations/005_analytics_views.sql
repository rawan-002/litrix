


BEGIN;


CREATE OR REPLACE VIEW v_paper_citations AS
SELECT
    rp."PaperID",
    rp."Title",
    rp."PubYear",
    rp."DOI",
    rp."Source",
    rp."JournalID",
    COALESCE(
        ("RawData_Log"->'cited_by'->>'value')::int,
        ("RawData_Log"->>'cited_by_count')::int,
        0
    ) AS citations
FROM "ResearchPaper" rp;

COMMENT ON VIEW v_paper_citations IS
    'Tabular view of per-paper citation counts. Hides the RawData_Log JSON shape.';


CREATE OR REPLACE VIEW v_researcher_h_index AS
WITH ranked AS (
    SELECT
        a."UserID",
        pc.citations,
        ROW_NUMBER() OVER (
            PARTITION BY a."UserID"
            ORDER BY pc.citations DESC, pc."PaperID"
        ) AS rnk
    FROM "Authors" a
    JOIN v_paper_citations pc ON pc."PaperID" = a."PaperID"
)
SELECT
    "UserID",
    COALESCE(MAX(LEAST(citations, rnk::int)), 0) AS h_index
FROM ranked
GROUP BY "UserID";

COMMENT ON VIEW v_researcher_h_index IS
    'Standard h-index: largest h where the researcher has h papers '
    'each cited at least h times. Computed with window functions.';


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
        FILTER (WHERE rp."Source" = 'Both')            AS cross_validated_papers
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

COMMENT ON VIEW v_researcher_stats IS
    'Cornerstone view: one row per researcher with all KPIs needed '
    'by the Researcher and HoD dashboards.';


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
    COALESCE(ROUND(AVG(rs.h_index)::numeric, 1), 0)    AS avg_h_index,
    COALESCE(MAX(rs.h_index), 0)                       AS max_h_index
FROM "Department" d
LEFT JOIN v_researcher_stats rs ON rs.department_id = d."DepartmentID"
GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID";

COMMENT ON VIEW v_department_stats IS
    'Per-department aggregations. Source for HoD landing page and the '
    'Dean cross-department comparison chart.';


CREATE OR REPLACE VIEW v_top_papers AS
SELECT
    rp."PaperID"                                       AS paper_id,
    rp."Title"                                         AS title,
    rp."PubYear"                                       AS pub_year,
    rp."DOI"                                           AS doi,
    rp."Source"                                        AS source,
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

COMMENT ON VIEW v_top_papers IS
    'Per-paper leaderboard view with citations + journal quality. '
    'Filter by department/researcher in queries; this view is unfiltered.';


CREATE OR REPLACE VIEW v_publication_trends AS
SELECT
    d."DepartmentID"                                   AS department_id,
    d."DepartmentName"                                 AS department_name,
    pc."PubYear"                                       AS year,
    COUNT(DISTINCT pc."PaperID")                       AS papers,
    COALESCE(SUM(pc.citations), 0)                     AS citations,
    COUNT(DISTINCT pc."PaperID")
        FILTER (WHERE jr."Quartile" = 'Q1')            AS q1_papers
FROM v_paper_citations pc
JOIN "Authors" a ON a."PaperID" = pc."PaperID"
JOIN "Works_In" w
    ON w."UserID" = a."UserID"
   AND w."IsCurrentPosition" = TRUE
JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
LEFT JOIN "JournalRankings" jr ON jr."JournalID" = pc."JournalID"
WHERE pc."PubYear" IS NOT NULL
GROUP BY d."DepartmentID", d."DepartmentName", pc."PubYear"
ORDER BY d."DepartmentName", pc."PubYear";

COMMENT ON VIEW v_publication_trends IS
    'Department × Year aggregation for trend charts. '
    'One row per (department, year) tuple with papers/citations/q1 counts.';


COMMIT;


