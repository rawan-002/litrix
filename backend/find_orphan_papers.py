"""
DEPRECATED — replaceable with a single SELECT.

Found ResearchPaper rows with no Authors entries (orphaned — usually
leftover from a partial scrape that didn't finish wiring up the
author→paper edges). Run this when needed:

    SELECT rp."PaperID", rp."Title", rp."PubYear"
    FROM "ResearchPaper" rp
    WHERE NOT EXISTS (
        SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
    )
    ORDER BY rp."ScrapedAt" DESC;
"""
raise SystemExit('find_orphan_papers.py is deprecated. See the docstring for the SQL.')
