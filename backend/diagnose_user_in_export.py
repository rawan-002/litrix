"""
DEPRECATED — debug aid for the Excel export.

The production export is now the source of truth:
    GET /api/export/excel/?years=2025

If a researcher is missing, the dashboard's per-researcher profile
will show the same data — open /profile/<litrix-id> and compare.
"""
raise SystemExit('diagnose_user_in_export.py is deprecated. Use the live export endpoint.')
