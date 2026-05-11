"""
DEPRECATED — applier from a one-time ghost-paper review pass.

Read ghost_papers_review.xlsx (manual KEEP / DELETE decisions) and
applied them to the DB:
    KEEP   → mark IsVerified=TRUE
    DELETE → remove the Authors row (paper stays if other authors)

If a new ghost-paper sweep is needed:
    1. Run the SQL in diagnose_ghost_papers.py into an .xlsx review file.
    2. Add a "decision" column (KEEP/DELETE).
    3. Write a fresh applier using openpyxl + raw SQL on Authors.
"""
raise SystemExit(
    'apply_ghost_decisions.py is deprecated. See the docstring if a new sweep is needed.'
)
