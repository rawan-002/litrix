"""
DEPRECATED — superseded by the Sync admin UI + API endpoint.

    POST /api/auth/sync/trigger/
    { "user_id": 42, "source": "scholar", "force": true }

Or open Admin → Sync in the web UI and click "Force re-sync" on the
target researcher. Both paths route through accounts.sync_views and
respect the 7-day cooldown unless force=true.
"""
raise SystemExit(
    'rescrape_user.py is deprecated. '
    'Use POST /api/auth/sync/trigger/ or the Sync admin page.'
)
