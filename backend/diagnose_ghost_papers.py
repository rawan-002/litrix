"""
DEPRECATED — diagnostic from the ghost-paper review workflow.

Identified "ghost" papers — Authors rows pointing at researchers but
where the paper looked suspicious (no DOI + unverified + low cite
count + missing journal). For occasional re-checks, run:

    SELECT u."FullName_Ar", rp."Title", rp."PubYear", rp."Source"
    FROM "ResearchPaper" rp
    JOIN "Authors" a ON a."PaperID" = rp."PaperID"
    JOIN "Users" u ON u."UserID" = a."UserID"
    WHERE rp."DOI" IS NULL
      AND rp."IsVerified" = FALSE
      AND COALESCE(
          ("RawData_Log"->'cited_by'->>'value')::int,
          0
      ) < 2
    ORDER BY u."FullName_Ar", rp."PubYear" DESC;
"""
raise SystemExit('diagnose_ghost_papers.py is deprecated. See the docstring for the SQL.')
