"""
DEPRECATED — one-shot bridge query.

Bridged papers that were scraped before their author had a Users row
by matching on Scholar_ID. The registration approval flow
(accounts/views.approve_registration) now handles the link forward.

For the historical case (Users imported AFTER their papers were
scraped), the equivalent SQL is:
    UPDATE "Authors" a
    SET "UserID" = u."UserID"
    FROM "Users" u, "ResearchPaper" rp
    WHERE a."PaperID" = rp."PaperID"
      AND a."UserID" IS NULL
      AND rp."RawData_Log"->>'citation_id' = u."Scholar_ID"
      AND u."Scholar_ID" IS NOT NULL;
"""
raise SystemExit(
    'link_papers_by_scholar.py is deprecated. See the docstring for the SQL equivalent.'
)
