"""
DEPRECATED — replaced by a Django management command.

This script's functionality now lives at:
    backend/accounts/management/commands/create_admin.py

Run it with:
    python manage.py create_admin
    python manage.py create_admin --email a@b.com --password 'P@ss!' --name "Name"

You can safely delete this file once you've confirmed the new command
works on your environment.
"""
raise SystemExit(
    'create_admin.py has moved to a Django management command. '
    'Run:  python manage.py create_admin'
)
