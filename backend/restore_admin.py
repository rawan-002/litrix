"""
DEPRECATED — replaced by a Django management command.

This script's functionality now lives at:
    backend/accounts/management/commands/restore_admin.py

Run it with:
    python manage.py restore_admin
    python manage.py restore_admin --reset-password 'NewP@ss!'

You can safely delete this file once you've confirmed the new command
works on your environment.
"""
raise SystemExit(
    'restore_admin.py has moved to a Django management command. '
    'Run:  python manage.py restore_admin'
)
