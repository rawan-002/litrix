


BEGIN;

DROP VIEW IF EXISTS v_paper_citations CASCADE;

CREATE VIEW v_paper_citations AS
SELECT
    rp."PaperID",
    rp."Title",
    rp."PubYear",
    rp."DOI",
    rp."Source",
    rp."JournalID",
    rp."CitationsByYear" AS citations_by_year,
    COALESCE(
        ("RawData_Log"->'cited_by'->>'value')::int,
        ("RawData_Log"->>'cited_by_count')::int,
        0
    ) AS citations
FROM "ResearchPaper" rp;

COMMIT;
