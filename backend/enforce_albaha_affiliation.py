"""
DEPRECATED — one-shot data-quality pass.

Removed Authors rows whose linked paper had zero authors affiliated
with Al-Baha University. The current scraper pipeline filters cross-
institution drift at ingest time, so this is no longer needed.

If you suspect drift again, run this audit first:
    SELECT u."FullName_Ar", COUNT(*)
    FROM "Authors" a
    JOIN "Users" u ON u."UserID" = a."UserID"
    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
    WHERE NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(rp."RawData_Log"->'authorships') aut
        CROSS JOIN jsonb_array_elements(aut->'institutions') inst
        WHERE inst->>'display_name' ILIKE '%al%baha%'
    )
    GROUP BY u."FullName_Ar"
    ORDER BY 2 DESC;
"""
raise SystemExit(
    'enforce_albaha_affiliation.py is deprecated. See the docstring for the audit query.'
)
