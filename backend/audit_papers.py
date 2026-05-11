"""
DEPRECATED — superseded by the dashboard endpoints.

What this used to print (papers per user, per quartile, etc.) is now
available via:
    /api/stats/overview/             — institution totals
    /api/researchers/{id}/profile/   — per-researcher breakdown
    /api/yearly-breakdown/?year=2025 — per-year per-dept
For one-off ad-hoc queries, use Django shell with raw SQL.
"""
raise SystemExit('audit_papers.py is deprecated. Use the /api/stats/ endpoints.')
