"""
DEPRECATED — superseded by the production export endpoint.

    GET /api/export/excel/
        ?years=2025,2026
        &sheets=summary,departments,researchers,journals,conferences

The endpoint covers everything this script did, plus dashboard-driven
year + sheet selection. Use it from the Dashboard UI (Export button)
or hit the URL directly.
"""
raise SystemExit(
    'export_with_affiliations.py is deprecated. '
    'Use GET /api/export/excel/ or the Dashboard Export button.'
)
